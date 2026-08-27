from __future__ import annotations

import os
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from sqlcipher3 import dbapi2 as sqlite3

from .services import (
    ANNUAL_EXPENSE_MONTHS, ANNUAL_EXPENSE_TYPE, EXPENSE_KIND, SETTLEMENT_KIND,
)
from .schema import CURRENT_SCHEMA_VERSION
from .session_security import AccessPermit, authorization_lease
from .vault import VaultLockedError, apply_key, database_password, database_state

ROOT = Path(__file__).resolve().parents[1]


def _packaged_data_dir(platform: str, home: Path, environment: dict[str, str]) -> Path:
    """Return the conventional per-user data directory for a packaged build."""
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Expensetics"
    if platform.startswith("win"):
        local_app_data = environment.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "Expensetics"
    xdg_data_home = environment.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else home / ".local" / "share"
    return base / "Expensetics"


def _default_data_dir() -> Path:
    configured = os.environ.get("EXPENSETICS_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return _packaged_data_dir(sys.platform, Path.home(), dict(os.environ))
    return ROOT / "data"


DATA_DIR = _default_data_dir()
DB_PATH = DATA_DIR / "finance.db"

CATEGORIES = (
    "Groceries", "Housing", "Bills & Utilities", "Transportation", "Health",
    "Dining", "Personal", "Shopping", "Entertainment", "Travel", "Other", "Gifts",
)

TRANSACTION_COLUMNS_SQL = f"""
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
    transaction_kind TEXT NOT NULL DEFAULT '{EXPENSE_KIND}'
        CHECK (transaction_kind IN ('{EXPENSE_KIND}', '{SETTLEMENT_KIND}')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    purpose TEXT NOT NULL DEFAULT '',
    expense_type TEXT NOT NULL DEFAULT 'Living'
        CHECK (expense_type IN ('Living', 'Discretionary', 'Travel', 'One-off')),
    need_want TEXT NOT NULL DEFAULT '' CHECK (need_want IN ('', 'Need', 'Want')),
    notes TEXT NOT NULL DEFAULT '',
    source_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    subcategory TEXT NOT NULL DEFAULT '',
    spread_months INTEGER NOT NULL DEFAULT 1 CHECK (spread_months BETWEEN 1 AND 120),
    source_bank TEXT NOT NULL DEFAULT '',
    source_vendor TEXT NOT NULL DEFAULT '',
    source_vendor_key TEXT NOT NULL DEFAULT '',
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    CHECK (
        (transaction_kind = '{EXPENSE_KIND}' AND amount_cents > 0)
        OR (transaction_kind = '{SETTLEMENT_KIND}' AND amount_cents < 0)
    )
"""


def authorization_required(path: Path, enforce: bool = False) -> bool:
    """Protect the application database; tests may opt other databases in too."""
    return enforce or path.resolve() == DB_PATH.resolve()


class _LeasedConnection:
    """Delegate SQLCipher access while retaining its authorization lease."""

    def __init__(self, connection: sqlite3.Connection, lease) -> None:
        self._connection = connection
        self._lease = lease
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            self._lease.__exit__(None, None, None)


def connect(
    path: Path = DB_PATH,
    *,
    permit: AccessPermit | None = None,
    require_authorization: bool = False,
) -> _LeasedConnection:
    lease = (
        authorization_lease(permit)
        if authorization_required(path, require_authorization)
        else nullcontext()
    )
    lease.__enter__()
    connection = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        state = database_state(path)
        if state == "encrypted":
            password = database_password()
            if password is None:
                connection.close()
                raise VaultLockedError("Unlock Expensetics before opening the database")
            apply_key(connection, password)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        return _LeasedConnection(connection, lease)
    except Exception:
        if connection is not None:
            connection.close()
        lease.__exit__(None, None, None)
        raise


@contextmanager
def transaction(
    path: Path = DB_PATH,
    *,
    permit: AccessPermit | None = None,
    require_authorization: bool = False,
) -> Iterator[sqlite3.Connection]:
    connection = connect(
        path, permit=permit, require_authorization=require_authorization,
    )
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize(
    path: Path = DB_PATH,
    *,
    permit: AccessPermit | None = None,
    require_authorization: bool = False,
) -> None:
    with transaction(
        path, permit=permit, require_authorization=require_authorization,
    ) as connection:
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version(version)
            SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS category_subcategories (
                id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(name)) > 0),
                sort_order INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category_id, name)
            );

            CREATE TABLE IF NOT EXISTS category_migration_log (
                id INTEGER PRIMARY KEY,
                source_category TEXT NOT NULL,
                source_subcategory TEXT,
                target_category TEXT NOT NULL,
                target_subcategory_action TEXT NOT NULL
                    CHECK (target_subcategory_action IN ('keep', 'clear', 'replace')),
                target_subcategory TEXT,
                affected_transactions INTEGER NOT NULL CHECK (affected_transactions >= 0),
                affected_vendor_mappings INTEGER NOT NULL CHECK (affected_vendor_mappings >= 0),
                first_transaction_date TEXT,
                last_transaction_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE
                    CHECK (length(trim(name)) > 0),
                account_type TEXT NOT NULL,
                institution TEXT NOT NULL DEFAULT '',
                last_four TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transactions (
                {TRANSACTION_COLUMNS_SQL}
            );

            CREATE TABLE IF NOT EXISTS bank_import_history (
                id INTEGER PRIMARY KEY,
                filename TEXT NOT NULL CHECK (length(trim(filename)) BETWEEN 1 AND 255),
                bank TEXT NOT NULL CHECK (length(trim(bank)) > 0),
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                first_transaction_date TEXT NOT NULL,
                last_transaction_date TEXT NOT NULL,
                source_row_count INTEGER NOT NULL CHECK (source_row_count > 0),
                selected_row_count INTEGER NOT NULL CHECK (selected_row_count > 0),
                imported_count INTEGER NOT NULL CHECK (
                    imported_count >= 0 AND imported_count <= selected_row_count
                ),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (first_transaction_date <= last_transaction_date)
            );
            CREATE INDEX IF NOT EXISTS idx_bank_import_history_created
                ON bank_import_history(created_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS income_entries (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                description TEXT NOT NULL CHECK (length(trim(description)) > 0),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_income_entries_date ON income_entries(date);

            CREATE TABLE IF NOT EXISTS net_worth_snapshots (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL UNIQUE,
                assets_cents INTEGER NOT NULL CHECK (assets_cents >= 0),
                liabilities_cents INTEGER NOT NULL CHECK (liabilities_cents >= 0),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_net_worth_snapshots_date ON net_worth_snapshots(date);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monthly_budgets (
                month TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
                category_id INTEGER NOT NULL REFERENCES categories(id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (month, category_id)
            );

            CREATE TABLE IF NOT EXISTS income_estimates (
                month TEXT PRIMARY KEY CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
                amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS liabilities (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                liability_type TEXT NOT NULL,
                original_principal_cents INTEGER NOT NULL CHECK (original_principal_cents > 0),
                annual_rate_bps INTEGER NOT NULL CHECK (annual_rate_bps >= 0),
                term_months INTEGER NOT NULL CHECK (term_months > 0),
                start_date TEXT NOT NULL,
                payment_cents INTEGER NOT NULL CHECK (payment_cents > 0),
                notes TEXT NOT NULL DEFAULT '',
                payment_match_key TEXT NOT NULL DEFAULT '',
                payment_match_label TEXT NOT NULL DEFAULT '',
                rate_type TEXT NOT NULL DEFAULT 'Fixed',
                interest_convention TEXT NOT NULL DEFAULT 'Monthly',
                rate_term_months INTEGER NOT NULL DEFAULT 60 CHECK (rate_term_months > 0),
                current_balance_cents INTEGER NOT NULL CHECK (current_balance_cents >= 0),
                balance_as_of_date TEXT NOT NULL,
                payment_frequency TEXT NOT NULL DEFAULT 'Monthly',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_liabilities_start_date ON liabilities(start_date);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(transactions)")}
        if "subcategory" not in columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN subcategory TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """UPDATE transactions
                   SET subcategory = substr(purpose, length('Original group: ') + 1)
                   WHERE purpose LIKE 'Original group: %' AND subcategory = ''"""
            )
        if "spread_months" not in columns:
            connection.execute(
                "ALTER TABLE transactions ADD COLUMN spread_months INTEGER NOT NULL DEFAULT 1 "
                "CHECK (spread_months BETWEEN 1 AND 120)"
            )
            connection.execute(
                "UPDATE transactions SET spread_months=? WHERE expense_type=?",
                (ANNUAL_EXPENSE_MONTHS, ANNUAL_EXPENSE_TYPE),
            )
        metadata_columns = {
            "source_bank": "TEXT NOT NULL DEFAULT ''",
            "source_vendor": "TEXT NOT NULL DEFAULT ''",
            "source_vendor_key": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in metadata_columns.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} {definition}")
        if "account_id" not in columns:
            connection.execute(
                "ALTER TABLE transactions ADD COLUMN account_id INTEGER "
                "REFERENCES accounts(id) ON DELETE SET NULL"
            )
        liability_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(liabilities)")
        }
        if "payment_match_key" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN payment_match_key TEXT NOT NULL DEFAULT ''"
            )
        if "payment_match_label" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN payment_match_label TEXT NOT NULL DEFAULT ''"
            )
        if "rate_type" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN rate_type TEXT NOT NULL DEFAULT 'Fixed'"
            )
        if "interest_convention" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN interest_convention TEXT NOT NULL DEFAULT 'Monthly'"
            )
        if "rate_term_months" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN rate_term_months INTEGER NOT NULL DEFAULT 60 "
                "CHECK (rate_term_months > 0)"
            )
        if "current_balance_cents" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN current_balance_cents INTEGER"
            )
            connection.execute(
                "UPDATE liabilities SET current_balance_cents=original_principal_cents"
            )
        if "balance_as_of_date" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN balance_as_of_date TEXT"
            )
            connection.execute(
                "UPDATE liabilities SET balance_as_of_date=date(start_date, '-1 day')"
            )
        if "payment_frequency" not in liability_columns:
            connection.execute(
                "ALTER TABLE liabilities ADD COLUMN payment_frequency "
                "TEXT NOT NULL DEFAULT 'Monthly'"
            )
        current_version = connection.execute("SELECT version FROM schema_version").fetchone()["version"]
        if "transaction_kind" not in columns:
            _migrate_transactions_v10(connection)
        if current_version < 4:
            connection.execute(
                """UPDATE transactions SET subcategory=description
                   WHERE source_key IS NOT NULL AND lower(subcategory) IN
                   ('grocery', 'amazon', 'cafe', 'eating out', 'lifestyle', 'subscriptions',
                    'bills', 'transportation', 'misc', 'occasions', 'vacation', 'clothing')"""
            )
        transaction_indexes = (
            "CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_description ON transactions(description)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_category_date "
            "ON transactions(category_id, date)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_duplicate "
            "ON transactions(date, amount_cents)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_account_date "
            "ON transactions(account_id, date)",
        )
        for statement in transaction_indexes:
            connection.execute(statement)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS vendor_mappings (
                   id INTEGER PRIMARY KEY,
                   bank TEXT NOT NULL,
                   vendor_key TEXT NOT NULL,
                   description TEXT NOT NULL,
                   category_id INTEGER NOT NULL REFERENCES categories(id),
                   subcategory TEXT NOT NULL DEFAULT '',
                   score REAL NOT NULL DEFAULT 1.0 CHECK (score >= 0),
                   uses INTEGER NOT NULL DEFAULT 1 CHECK (uses > 0),
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(bank, vendor_key, description, category_id, subcategory)
               )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vendor_mapping_lookup "
            "ON vendor_mappings(bank, vendor_key, score DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_category_subcategories_category "
            "ON category_subcategories(category_id, is_active, sort_order)"
        )
        connection.execute("PRAGMA optimize")
        if connection.execute("SELECT 1 FROM categories LIMIT 1").fetchone() is None:
            connection.executemany(
                "INSERT INTO categories(name, sort_order) VALUES (?, ?)",
                [(name, index) for index, name in enumerate(CATEGORIES)],
            )
        connection.execute(
            """INSERT OR IGNORE INTO category_subcategories(category_id, name, sort_order)
               SELECT category_id, MIN(trim(subcategory)), 1000 + MIN(id)
               FROM transactions WHERE trim(subcategory) != ''
               GROUP BY category_id, lower(trim(subcategory))"""
        )
        if current_version < 9:
            connection.execute(
                "UPDATE transactions SET spread_months=? WHERE expense_type=?",
                (ANNUAL_EXPENSE_MONTHS, ANNUAL_EXPENSE_TYPE),
            )
        connection.execute(
            "UPDATE schema_version SET version = ?", (CURRENT_SCHEMA_VERSION,),
        )


def _migrate_transactions_v10(connection: sqlite3.Connection) -> None:
    """Rebuild the table once so signed settlements have a strict invariant."""
    connection.execute(f"CREATE TABLE transactions_v10 ({TRANSACTION_COLUMNS_SQL})")
    connection.execute(
        f"""INSERT INTO transactions_v10 (
                id, date, amount_cents, transaction_kind, description, category_id,
                purpose, expense_type, need_want, notes, source_key, created_at,
                updated_at, subcategory, spread_months, source_bank, source_vendor,
                source_vendor_key, account_id
            )
            SELECT
                id, date, amount_cents, '{EXPENSE_KIND}', description, category_id,
                purpose, expense_type, need_want, notes, source_key, created_at,
                updated_at, subcategory, spread_months, source_bank, source_vendor,
                source_vendor_key, account_id
            FROM transactions"""
    )
    connection.execute("DROP TABLE transactions")
    connection.execute("ALTER TABLE transactions_v10 RENAME TO transactions")
