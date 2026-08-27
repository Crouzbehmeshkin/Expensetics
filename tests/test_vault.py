from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

import finance_app.vault as vault_module
from finance_app.db import connect, initialize
from finance_app.export import export_encrypted_backup
from finance_app.models import TransactionInput
from finance_app.repository import Repository
from finance_app.vault import (
    VaultError, VaultLockedError, change_password, database_state, delete_vault,
    lock, open_encrypted, prepare, recover_encrypted_candidate,
    recover_interrupted_migration,
    remove_legacy_csvs, restore_backup, unlock,
)


PASSWORD = "correct horse battery staple"


def test_plaintext_database_migrates_only_after_encrypted_copy_is_verified(tmp_path) -> None:
    database = tmp_path / "finance.db"
    initialize(database)
    repository = Repository(database)
    repository.add(TransactionInput(
        date(2026, 8, 5), Decimal("42.10"), "Migration check", "Other",
    ))
    assert database_state(database) == "plaintext"

    assert prepare(database, PASSWORD, PASSWORD) == "migrated"
    assert database_state(database) == "encrypted"
    assert Repository(database).list("2026-08")[0]["description"] == "Migration check"
    encrypted_connection = open_encrypted(database, PASSWORD)
    assert encrypted_connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert encrypted_connection.execute("PRAGMA temp_store").fetchone()[0] == 2
    encrypted_connection.close()
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(database).execute("SELECT * FROM transactions").fetchone()

    lock()
    with pytest.raises(VaultLockedError):
        connect(database)
    with pytest.raises(VaultError, match="incorrect"):
        unlock(database, "wrong password")
    unlock(database, PASSWORD)
    assert Repository(database).summary("2026-08")["total"] == 4210


def test_encrypted_backup_has_no_plaintext_sqlite_header_and_needs_its_password(tmp_path) -> None:
    database = tmp_path / "finance.db"
    assert prepare(database, PASSWORD, PASSWORD) == "created"
    initialize(database)
    Repository(database).add(TransactionInput(
        date(2026, 8, 6), Decimal("18.75"), "Encrypted record", "Shopping",
    ))

    backup = export_encrypted_backup(
        tmp_path / "portable", "separate backup password", database,
    )
    assert backup.suffix == ".expensetics"
    assert database_state(backup) == "encrypted"
    assert b"Encrypted record" not in backup.read_bytes()
    with pytest.raises(VaultError, match="incorrect"):
        unlock(backup, "wrong backup password")
    with pytest.raises(VaultError, match="incorrect"):
        restore_backup(database, backup, "wrong backup password")
    assert not (tmp_path / "finance.db.restore.tmp").exists()
    assert Repository(database).summary("2026-08")["total"] == 1875


def test_backup_does_not_silently_replace_an_existing_file(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    destination = tmp_path / "portable.expensetics"
    destination.write_bytes(b"existing backup")

    with pytest.raises(ValueError, match="already exists"):
        export_encrypted_backup(destination, "separate backup password", database)

    assert destination.read_bytes() == b"existing backup"


def test_backup_preserves_unrelated_sibling_temp_file(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    destination = tmp_path / "portable.expensetics"
    unrelated = tmp_path / "portable.expensetics.tmp"
    unrelated.write_bytes(b"unrelated user file")

    export_encrypted_backup(destination, "separate backup password", database)

    assert unrelated.read_bytes() == b"unrelated user file"
    assert destination.exists()


def test_restore_rejects_incompatible_pre_v1_backup_before_replacing_data(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    Repository(database).add(TransactionInput(
        date(2026, 8, 6), Decimal("18.75"), "Current record", "Shopping",
    ))
    backup_password = "separate backup password"
    backup = export_encrypted_backup(tmp_path / "old", backup_password, database)
    connection = open_encrypted(backup, backup_password)
    with connection:
        connection.execute("UPDATE schema_version SET version = 14")
    connection.close()

    with pytest.raises(VaultError, match="incompatible app build"):
        restore_backup(database, backup, backup_password)

    assert Repository(database).summary("2026-08")["total"] == 1875
    assert not (tmp_path / "finance.db.restore.tmp").exists()


def test_restore_rejects_an_encrypted_non_expensetics_database(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    backup_password = "separate backup password"
    backup = tmp_path / "unrelated.expensetics"
    connection = open_encrypted(backup, backup_password)
    with connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.close()

    with pytest.raises(VaultError, match="not a compatible Expensetics vault"):
        restore_backup(database, backup, backup_password)

    assert Repository(database).categories()


def test_migration_rolls_back_if_post_swap_verification_fails(tmp_path, monkeypatch) -> None:
    database = tmp_path / "finance.db"
    initialize(database)
    Repository(database).add(TransactionInput(
        date(2026, 8, 7), Decimal("9.40"), "Rollback record", "Other",
    ))
    original_verify = vault_module._verify
    call_count = 0

    def fail_second_verification(connection) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise VaultError("simulated post-swap failure")
        original_verify(connection)

    monkeypatch.setattr(vault_module, "_verify", fail_second_verification)
    with pytest.raises(VaultError, match="simulated"):
        prepare(database, PASSWORD, PASSWORD)
    assert database_state(database) == "plaintext"
    assert Repository(database).summary("2026-08")["total"] == 940
    assert not (tmp_path / "finance.db.encrypted.tmp").exists()
    assert not (tmp_path / "finance.db.plaintext.tmp").exists()


def test_password_change_rekeys_database_and_invalidates_old_password(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    change_password(database, PASSWORD, "a completely different password", "a completely different password")
    lock()
    with pytest.raises(VaultError, match="incorrect"):
        unlock(database, PASSWORD)
    unlock(database, "a completely different password")
    assert connect(database).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_interrupted_migration_restores_plaintext_before_retry(tmp_path) -> None:
    database = tmp_path / "finance.db"
    initialize(database)
    Repository(database).add(TransactionInput(
        date(2026, 8, 8), Decimal("7.25"), "Recover me", "Other",
    ))
    plaintext = tmp_path / "finance.db.plaintext.tmp"
    encrypted = tmp_path / "finance.db.encrypted.tmp"
    database.replace(plaintext)
    encrypted.write_bytes(b"incomplete encrypted candidate")

    recover_interrupted_migration(database)

    assert database_state(database) == "plaintext"
    assert Repository(database).summary("2026-08")["total"] == 725
    assert not plaintext.exists()
    assert not encrypted.exists()


def test_verified_unlock_removes_plaintext_migration_remnant(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    plaintext = tmp_path / "finance.db.plaintext.tmp"
    connection = sqlite3.connect(plaintext)
    with connection:
        connection.execute("CREATE TABLE leftover(value INTEGER)")
    connection.close()

    lock()
    recover_interrupted_migration(database)
    assert plaintext.exists()
    unlock(database, PASSWORD)
    assert not plaintext.exists()


def test_lone_encrypted_candidate_is_preserved_for_explicit_recovery(tmp_path) -> None:
    database = tmp_path / "finance.db"
    candidate = tmp_path / "finance.db.encrypted.tmp"
    candidate.write_bytes(b"candidate")

    with pytest.raises(VaultError, match="interrupted encryption candidate"):
        recover_interrupted_migration(database)

    assert candidate.exists()
    assert not database.exists()


def test_verified_encrypted_candidate_can_be_promoted(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    Repository(database).add(TransactionInput(
        date(2026, 8, 9), Decimal("14.50"), "Candidate record", "Other",
    ))
    candidate = tmp_path / "finance.db.encrypted.tmp"
    lock()
    database.replace(candidate)

    with pytest.raises(VaultError, match="incorrect"):
        recover_encrypted_candidate(database, "wrong password")
    assert candidate.exists() and not database.exists()

    recover_encrypted_candidate(database, PASSWORD)
    assert database_state(database) == "encrypted"
    assert Repository(database).summary("2026-08")["total"] == 1450
    assert not candidate.exists()


def test_legacy_csv_cleanup_and_vault_deletion_verify_paths_are_gone(tmp_path) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    initialize(database)
    transactions = tmp_path / "transactions.csv"
    categories = tmp_path / "categories.csv"
    unrelated = tmp_path / "notes.txt"
    for path in (transactions, categories, unrelated):
        path.write_text("sensitive", encoding="utf-8")

    removed_csvs = remove_legacy_csvs(tmp_path)
    assert set(removed_csvs) == {transactions, categories}
    assert not transactions.exists() and not categories.exists()
    assert unrelated.exists()

    backups = tmp_path / "backups"
    backups.mkdir()
    plaintext_backup = backups / "old.db"
    connection = sqlite3.connect(plaintext_backup)
    with connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
    connection.close()
    unrelated_backup = backups / "notes.txt"
    unrelated_backup.write_text("keep", encoding="utf-8")
    assert plaintext_backup.exists()
    assert unrelated_backup.exists()

    remnants = {
        tmp_path / "finance.db.encrypted.tmp",
        tmp_path / "finance.db.plaintext.tmp",
        tmp_path / "finance.db.restore.tmp",
        tmp_path / "finance.db.restore.tmp-journal",
    }
    for remnant in remnants:
        remnant.write_text("sensitive", encoding="utf-8")

    removed_vault = delete_vault(database)
    assert set(removed_vault) == {database, *remnants}
    assert all(not candidate.exists() for candidate in removed_vault)


def test_vault_deletion_continues_after_one_file_cannot_be_removed(
    tmp_path, monkeypatch,
) -> None:
    database = tmp_path / "finance.db"
    prepare(database, PASSWORD, PASSWORD)
    remnant = tmp_path / "finance.db.plaintext.tmp"
    remnant.write_text("sensitive", encoding="utf-8")
    original_remove = vault_module.secure_remove

    def fail_active_vault(candidate) -> bool:
        if candidate == database:
            raise OSError("simulated locked file")
        return original_remove(candidate)

    monkeypatch.setattr(vault_module, "secure_remove", fail_active_vault)
    with pytest.raises(VaultError, match="could not be removed"):
        delete_vault(database)

    assert database.exists()
    assert not remnant.exists()
    assert not vault_module.is_unlocked()


def test_secure_remove_never_overwrites_another_hard_link(tmp_path) -> None:
    target = tmp_path / "keep.txt"
    candidate = tmp_path / "remove.txt"
    target.write_bytes(b"keep this content")
    try:
        candidate.hardlink_to(target)
    except OSError as error:
        pytest.skip(f"Hard links are not available: {error}")

    assert vault_module.secure_remove(candidate)

    assert not candidate.exists()
    assert target.read_bytes() == b"keep this content"


def test_secure_remove_unlinks_a_symlink_without_touching_its_target(tmp_path) -> None:
    target = tmp_path / "keep.txt"
    candidate = tmp_path / "remove.txt"
    target.write_bytes(b"keep this content")
    try:
        candidate.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symbolic links are not available: {error}")

    assert vault_module.secure_remove(candidate)

    assert not candidate.exists()
    assert target.read_bytes() == b"keep this content"


def test_secure_remove_symlink_branch_without_platform_privileges(
    tmp_path, monkeypatch,
) -> None:
    candidate = tmp_path / "simulated-link"
    symlink_states = iter((True, False))
    unlinked = []
    path_type = type(candidate)

    monkeypatch.setattr(path_type, "is_symlink", lambda path: next(symlink_states))
    monkeypatch.setattr(path_type, "unlink", lambda path: unlinked.append(path))

    assert vault_module.secure_remove(candidate)
    assert unlinked == [candidate]
