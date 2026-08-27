from __future__ import annotations

from pathlib import Path

from .db import DB_PATH
from .vault import create_backup, restore_backup


def export_encrypted_backup(
    destination: str | Path,
    password: str,
    db_path: Path = DB_PATH,
) -> Path:
    """Create an integrity-checked SQLCipher snapshot with a separate password."""
    return create_backup(db_path, Path(destination), password)


def restore_encrypted_backup(
    source: str | Path,
    password: str,
    db_path: Path = DB_PATH,
) -> None:
    """Replace the active vault with a verified, re-encrypted snapshot."""
    restore_backup(db_path, Path(source), password)
