from __future__ import annotations

import os
from pathlib import Path
import tempfile

from sqlcipher3 import dbapi2 as sqlcipher

from .schema import CURRENT_SCHEMA_VERSION


BACKUP_EXTENSION = ".expensetics"
LEGACY_CSV_NAMES = (
    "accounts.csv",
    "budgets.csv",
    "categories.csv",
    "income.csv",
    "income_estimates.csv",
    "liabilities.csv",
    "net_worth.csv",
    "transactions.csv",
)


class VaultError(RuntimeError):
    """Raised when an encrypted database cannot be opened or migrated safely."""


class VaultLockedError(VaultError):
    """Raised when database access is attempted before the vault is unlocked."""


_database_password: str | None = None


def database_password() -> str | None:
    return _database_password


def is_unlocked() -> bool:
    return _database_password is not None


def lock() -> None:
    global _database_password
    _database_password = None


def _migration_paths(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(path.name + ".encrypted.tmp"),
        path.with_name(path.name + ".plaintext.tmp"),
    )


def recover_interrupted_migration(path: Path) -> None:
    """Restore the last known plaintext database after an interrupted swap.

    An encrypted candidate is never promoted without password verification. If it
    is the only surviving file, it is preserved for explicit recovery instead of
    silently creating a new empty vault.
    """
    encrypted, plaintext = _migration_paths(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        if plaintext.exists():
            plaintext.replace(path)
            secure_remove(encrypted)
            return
        if encrypted.exists():
            raise VaultError(
                "An interrupted encryption candidate was found. Enter the vault "
                "password to verify and recover it."
            )
        return

    state = database_state(path)
    if state == "plaintext":
        secure_remove(encrypted)
        secure_remove(plaintext)
    elif state == "encrypted":
        secure_remove(encrypted)


def finalize_interrupted_migration(path: Path) -> None:
    """Remove migration remnants only after the active vault was verified."""
    encrypted, plaintext = _migration_paths(path)
    secure_remove(encrypted)
    secure_remove(plaintext)


def recover_encrypted_candidate(path: Path, password: str) -> None:
    """Promote the sole migration candidate only after password verification."""
    if path.exists():
        raise VaultError("The active vault already exists")
    encrypted, plaintext = _migration_paths(path)
    if plaintext.exists() or not encrypted.exists():
        raise VaultError("No encrypted migration candidate is ready for recovery")
    candidate = open_encrypted(encrypted, password)
    try:
        _verify(candidate)
    finally:
        candidate.close()
    encrypted.replace(path)
    try:
        unlock(path, password)
    except Exception:
        if path.exists() and not encrypted.exists():
            path.replace(encrypted)
        raise


def database_state(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return "missing"
    with path.open("rb") as handle:
        header = handle.read(16)
    return "plaintext" if header == b"SQLite format 3\x00" else "encrypted"


def validate_new_password(password: str, confirmation: str) -> None:
    if password != confirmation:
        raise ValueError("Passwords do not match")
    if len(password) < 12:
        raise ValueError("Use at least 12 characters")


def _quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def apply_key(connection, password: str) -> None:
    connection.execute(f"PRAGMA key={_quoted(password)}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA temp_store=MEMORY")


def open_encrypted(path: Path, password: str):
    connection = sqlcipher.connect(path)
    connection.row_factory = sqlcipher.Row
    apply_key(connection, password)
    return connection


def _verify(connection) -> None:
    try:
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        result = connection.execute("PRAGMA cipher_integrity_check").fetchone()
    except sqlcipher.DatabaseError as error:
        raise VaultError("The password is incorrect or this is not a valid Expensetics vault") from error
    if result is not None and result[0] not in (None, "ok"):
        raise VaultError("The encrypted database failed its integrity check")


def unlock(path: Path, password: str) -> None:
    if not password:
        raise ValueError("Enter your password")
    recover_interrupted_migration(path)
    state = database_state(path)
    if state != "encrypted":
        raise VaultError("This database has not been encrypted yet")
    connection = open_encrypted(path, password)
    try:
        _verify(connection)
    finally:
        connection.close()
    global _database_password
    _database_password = password
    finalize_interrupted_migration(path)


def prepare(path: Path, password: str, confirmation: str) -> str:
    """Create or migrate a vault, returning ``created`` or ``migrated``."""
    validate_new_password(password, confirmation)
    recover_interrupted_migration(path)
    state = database_state(path)
    if state == "encrypted":
        raise VaultError("This vault already has a password")
    if state == "plaintext":
        _migrate_plaintext(path, password)
        result = "migrated"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_encrypted(path, password)
        try:
            connection.execute("CREATE TABLE vault_bootstrap(value INTEGER)")
            connection.execute("DROP TABLE vault_bootstrap")
            connection.commit()
        finally:
            connection.close()
        result = "created"
    global _database_password
    _database_password = password
    return result


def _migrate_plaintext(path: Path, password: str) -> None:
    encrypted, plaintext = _migration_paths(path)
    encrypted.unlink(missing_ok=True)
    plaintext.unlink(missing_ok=True)

    try:
        source = sqlcipher.connect(path)
        try:
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source.execute(
                f"ATTACH DATABASE {_quoted(encrypted)} AS encrypted KEY {_quoted(password)}"
            )
            source.execute("SELECT sqlcipher_export('encrypted')")
            source.execute("DETACH DATABASE encrypted")
        finally:
            source.close()

        candidate = open_encrypted(encrypted, password)
        try:
            _verify(candidate)
        finally:
            candidate.close()

        path.replace(plaintext)
        try:
            encrypted.replace(path)
            check = open_encrypted(path, password)
            try:
                _verify(check)
            finally:
                check.close()
        except Exception:
            secure_remove(path)
            if plaintext.exists():
                plaintext.replace(path)
            raise
    except Exception:
        secure_remove(encrypted)
        if not path.exists() and plaintext.exists():
            plaintext.replace(path)
        raise
    secure_remove(plaintext)
    for suffix in ("-wal", "-shm", "-journal"):
        secure_remove(Path(str(path) + suffix))


def secure_remove(path: Path) -> bool:
    """Remove an accessible file and verify the directory entry is gone.

    This is best-effort overwriting for legacy plaintext. Modern SSD wear leveling
    can retain physical blocks, so ongoing protection relies on encryption and
    cryptographic key loss rather than claims of physical-sector erasure.
    """
    if path.is_symlink():
        path.unlink()
        if path.is_symlink():
            raise VaultError(f"Could not remove {path}")
        return True
    if not path.exists():
        return False
    if path.is_file() and path.stat().st_nlink == 1:
        size = path.stat().st_size
        try:
            with path.open("r+b", buffering=0) as handle:
                remaining = size
                block = b"\x00" * min(1024 * 1024, max(1, size))
                while remaining:
                    chunk = block[: min(len(block), remaining)]
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
    path.unlink(missing_ok=True)
    if path.exists() or path.is_symlink():
        raise VaultError(f"Could not remove {path}")
    return True


def remove_legacy_csvs(data_directory: Path) -> list[Path]:
    """Remove only known legacy mirrors from the app-owned data directory."""
    removed: list[Path] = []
    directory = data_directory.resolve()
    if not directory.exists() or not directory.is_dir():
        return removed
    for name in LEGACY_CSV_NAMES:
        candidate = directory / name
        if secure_remove(candidate):
            removed.append(candidate)
    return removed


def create_backup(database_path: Path, destination: Path, export_password: str) -> Path:
    validate_new_password(export_password, export_password)
    current_password = database_password()
    if current_password is None:
        raise VaultLockedError("Unlock Expensetics before creating a backup")
    destination = destination.expanduser().resolve()
    if destination.suffix.lower() != BACKUP_EXTENSION:
        destination = destination.with_suffix(BACKUP_EXTENSION)
    if destination.resolve() == database_path.resolve():
        raise ValueError("Choose a different backup path")
    if destination.exists():
        raise ValueError("A backup already exists at this path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        source = open_encrypted(database_path, current_password)
        try:
            source.execute(
                f"ATTACH DATABASE {_quoted(temporary)} AS backup KEY {_quoted(export_password)}"
            )
            source.execute("SELECT sqlcipher_export('backup')")
            source.execute("DETACH DATABASE backup")
        finally:
            source.close()
        check = open_encrypted(temporary, export_password)
        try:
            _verify(check)
        finally:
            check.close()
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise ValueError("A backup already exists at this path") from None
        temporary.unlink()
        for suffix in ("-journal", "-wal", "-shm"):
            secure_remove(Path(str(temporary) + suffix))
    except Exception:
        secure_remove(temporary)
        for suffix in ("-journal", "-wal", "-shm"):
            secure_remove(Path(str(temporary) + suffix))
        raise
    return destination


def restore_backup(database_path: Path, backup_path: Path, export_password: str) -> None:
    current_password = database_password()
    if current_password is None:
        raise VaultLockedError("Unlock Expensetics before restoring a backup")
    backup_path = backup_path.expanduser().resolve()
    if backup_path.suffix.lower() != BACKUP_EXTENSION:
        raise ValueError("Choose an .expensetics backup")
    if not backup_path.is_file():
        raise ValueError("Choose an existing .expensetics backup")
    replacement = database_path.with_name(database_path.name + ".restore.tmp")
    secure_remove(replacement)
    try:
        source = open_encrypted(backup_path, export_password)
        try:
            _verify(source)
            try:
                version_row = source.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()
            except sqlcipher.DatabaseError as error:
                raise VaultError(
                    "This backup is not a compatible Expensetics vault"
                ) from error
            if version_row is None or version_row[0] != CURRENT_SCHEMA_VERSION:
                raise VaultError(
                    "This pre-v1 backup was created by an incompatible app build"
                )
            source.execute(
                f"ATTACH DATABASE {_quoted(replacement)} AS restored KEY {_quoted(current_password)}"
            )
            source.execute("SELECT sqlcipher_export('restored')")
            source.execute("DETACH DATABASE restored")
        finally:
            source.close()
        check = open_encrypted(replacement, current_password)
        try:
            _verify(check)
        finally:
            check.close()
        replacement.replace(database_path)
    except Exception:
        secure_remove(replacement)
        raise


def change_password(path: Path, old_password: str, new_password: str, confirmation: str) -> None:
    validate_new_password(new_password, confirmation)
    connection = open_encrypted(path, old_password)
    try:
        _verify(connection)
        connection.execute(f"PRAGMA rekey={_quoted(new_password)}")
    finally:
        connection.close()
    unlock(path, new_password)


def delete_vault(path: Path) -> list[Path]:
    lock()
    removed: list[Path] = []
    failed = False
    encrypted, plaintext = _migration_paths(path)
    database_files = (
        path,
        encrypted,
        plaintext,
        path.with_name(path.name + ".restore.tmp"),
    )
    for database_file in database_files:
        for candidate in (
            database_file,
            Path(str(database_file) + "-wal"),
            Path(str(database_file) + "-shm"),
            Path(str(database_file) + "-journal"),
        ):
            try:
                if secure_remove(candidate):
                    removed.append(candidate)
            except (OSError, VaultError):
                failed = True
    if failed:
        raise VaultError("One or more vault files could not be removed")
    return removed
