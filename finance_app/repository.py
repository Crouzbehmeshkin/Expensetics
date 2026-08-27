from __future__ import annotations

from collections import defaultdict
from calendar import monthrange
from contextlib import contextmanager, nullcontext
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from sqlite3 import Connection, Cursor
from typing import Iterator, TypeVar

from .db import DB_PATH, authorization_required, connect, transaction
from .models import (
    AccountInput, BankImportMetadata, IncomeEstimateInput, IncomeInput, LiabilityInput,
    NetWorthInput, TransactionInput,
)
from .insights import build_transaction_insights
from .services import (
    ACCOUNT_TYPES, ANNUAL_EXPENSE_MONTHS, ANNUAL_EXPENSE_TYPE, allocate_cents, month_bounds,
    INCOME_FORECAST_MONTHS, INTEREST_CONVENTIONS, LIABILITY_TYPES, MORTGAGE_RATE_TYPES,
    PAYMENT_FREQUENCIES, loan_payment_cents, payment_monthly_equivalent,
    balance_after_payments_cents, exponential_average_cents, projected_payoff_months,
    normalize_description, normalize_institution, parse_amount, parse_nonnegative_amount,
    parse_transaction_amount, scheduled_payments_due, shifted_month, validate_month,
    weighted_income_forecast,
)
from .session_security import AccessPermit, authorization_lease, maintenance_lease

TRANSACTION_INSERT_SQL = """INSERT INTO transactions
    (date, amount_cents, transaction_kind, description, category_id,
     subcategory, purpose, expense_type, need_want, notes, spread_months,
     account_id, source_key, source_bank, source_vendor, source_vendor_key)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
SQLITE_BIND_CHUNK = 900
CATEGORY_NAME_LIMIT = 60
SUBCATEGORY_NAME_LIMIT = 80
TOTAL_MONTHLY_BUDGET_KEY = "total_monthly_budget_cents"
T = TypeVar("T")
_UNCHANGED = object()


def _chunks(values: list[T], size: int = SQLITE_BIND_CHUNK) -> Iterator[list[T]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class Repository:
    def __init__(
        self,
        db_path: Path = DB_PATH,
        *,
        permit: AccessPermit | None = None,
        require_authorization: bool = False,
        connection: Connection | None = None,
    ):
        self.db_path = db_path
        self.permit = permit
        self.require_authorization = require_authorization
        self._shared_connection = connection

    def _connect(self):
        if self._shared_connection is not None:
            return nullcontext(self._shared_connection)
        return connect(
            self.db_path,
            permit=self.permit,
            require_authorization=self.require_authorization,
        )

    def _transaction(self):
        if self._shared_connection is not None:
            raise RuntimeError("A read session cannot perform database mutations")
        return transaction(
            self.db_path,
            permit=self.permit,
            require_authorization=self.require_authorization,
        )

    @contextmanager
    def read_session(self):
        """Share one short-lived encrypted connection across a read operation."""
        if self._shared_connection is not None:
            yield self
            return
        with self._connect() as connection:
            yield Repository(
                self.db_path,
                permit=self.permit,
                require_authorization=self.require_authorization,
                connection=connection,
            )

    def authorization(self):
        """Hold this repository's session capability across a non-SQL operation."""
        if not authorization_required(self.db_path, self.require_authorization):
            return nullcontext()
        return authorization_lease(self.permit)

    def maintenance(self):
        """Exclude ordinary data access during a vault-wide mutation."""
        if not authorization_required(self.db_path, self.require_authorization):
            return nullcontext()
        return maintenance_lease(self.permit)

    @staticmethod
    def _require_record(cursor: Cursor, message: str) -> None:
        if cursor.rowcount != 1:
            raise ValueError(message)

    def categories(self) -> list[str]:
        with self._connect() as connection:
            return [row["name"] for row in connection.execute(
                "SELECT name FROM categories WHERE is_active = 1 ORDER BY sort_order"
            )]

    def accounts(self, *, include_inactive: bool = False) -> list[dict]:
        clause = "" if include_inactive else "WHERE a.is_active=1"
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT a.*, COUNT(t.id) transaction_count, MAX(t.date) last_used
                    FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id
                    {clause}
                    GROUP BY a.id ORDER BY a.is_active DESC, lower(a.name)"""
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def matching_accounts(accounts: list[dict], bank: str) -> list[dict]:
        bank_key = normalize_institution(bank)
        return [
            account for account in accounts
            if account["institution"]
            and normalize_institution(account["institution"]) == bank_key
        ]

    def save_account(self, item: AccountInput, identifier: int | None = None) -> int:
        name = item.name.strip()
        institution = item.institution.strip()
        last_four = item.last_four.strip()
        if not name:
            raise ValueError("Enter an account name")
        if item.account_type not in ACCOUNT_TYPES:
            raise ValueError("Choose an account type")
        if last_four and (len(last_four) != 4 or not last_four.isdigit()):
            raise ValueError("Last four digits must contain exactly four numbers")
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT id FROM accounts WHERE lower(name)=lower(?) AND id IS NOT ?",
                (name, identifier),
            ).fetchone()
            if duplicate:
                raise ValueError("Use a unique account name")
            if identifier is None:
                cursor = connection.execute(
                    """INSERT INTO accounts(name, account_type, institution, last_four, is_active)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, item.account_type, institution, last_four, int(item.is_active)),
                )
                identifier = int(cursor.lastrowid)
            else:
                cursor = connection.execute(
                    """UPDATE accounts SET name=?, account_type=?, institution=?, last_four=?,
                              is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (name, item.account_type, institution, last_four,
                     int(item.is_active), identifier),
                )
                self._require_record(cursor, "Account no longer exists")
        return identifier

    def set_account_active(self, identifier: int, active: bool) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE accounts SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(active), identifier),
            )
            self._require_record(cursor, "Account no longer exists")

    def category_settings(self) -> list[dict]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id, name, sort_order, is_active FROM categories ORDER BY sort_order, id"
            )]

    def category_library(self) -> list[dict]:
        """Return category definitions, usage, and their reusable subcategories."""
        with self._connect() as connection:
            categories = [dict(row) for row in connection.execute(
                """SELECT c.id, c.name, c.sort_order, c.is_active,
                          COUNT(t.id) transaction_count
                   FROM categories c LEFT JOIN transactions t ON t.category_id=c.id
                   GROUP BY c.id ORDER BY c.is_active DESC, c.sort_order, c.id"""
            )]
            subcategories = [dict(row) for row in connection.execute(
                """SELECT s.id, s.category_id, s.name, s.sort_order, s.is_active,
                          COUNT(t.id) transaction_count
                   FROM category_subcategories s
                   LEFT JOIN transactions t ON t.category_id=s.category_id
                    AND lower(trim(t.subcategory))=lower(s.name)
                   GROUP BY s.id ORDER BY s.category_id, s.is_active DESC,
                            s.sort_order, lower(s.name)"""
            )]
        by_category: dict[int, list[dict]] = defaultdict(list)
        for subcategory in subcategories:
            by_category[subcategory["category_id"]].append(subcategory)
        for category in categories:
            category["subcategories"] = by_category[category["id"]]
        return categories

    def subcategory_options(self, category: str) -> list[str]:
        with self._connect() as connection:
            return [row["name"] for row in connection.execute(
                """SELECT s.name FROM category_subcategories s
                   JOIN categories c ON c.id=s.category_id
                   WHERE c.name=? AND s.is_active=1
                   ORDER BY s.sort_order, lower(s.name)""",
                (category,),
            )]

    @staticmethod
    def _clean_category_label(value: object, label: str, limit: int) -> str:
        cleaned = " ".join(str(value or "").split())
        if not cleaned:
            raise ValueError(f"Enter a {label.lower()}")
        if len(cleaned) > limit:
            raise ValueError(f"{label} must be {limit} characters or fewer")
        if any(ord(character) < 32 for character in cleaned):
            raise ValueError(f"{label} contains unsupported characters")
        if label == "Category name" and cleaned.casefold() == "all":
            raise ValueError("Category name 'All' is reserved for filtering")
        if "subcategory" in label.casefold() and cleaned.casefold() == "__all__":
            raise ValueError("Use a different subcategory name")
        return cleaned

    @staticmethod
    def _category_by_id(connection: Connection, identifier: int) -> dict:
        row = connection.execute(
            "SELECT id, name, sort_order, is_active FROM categories WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row:
            raise ValueError("Category no longer exists")
        return dict(row)

    @staticmethod
    def _unique_category_name(
        connection: Connection, name: str, *, excluding: int | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT id FROM categories WHERE lower(name)=lower(?) AND id IS NOT ?",
            (name, excluding),
        ).fetchone()
        if row:
            raise ValueError("Use a unique category name")

    @classmethod
    def _insert_subcategories(
        cls, connection: Connection, category_id: int, names: list[str] | tuple[str, ...],
        *, reactivate_existing: bool = False,
    ) -> None:
        existing = {
            row["name"].casefold(): dict(row) for row in connection.execute(
                "SELECT id, name, is_active FROM category_subcategories WHERE category_id=?",
                (category_id,),
            )
        }
        next_order = connection.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 value "
            "FROM category_subcategories WHERE category_id=?",
            (category_id,),
        ).fetchone()["value"]
        for value in names:
            name = cls._clean_category_label(
                value, "Subcategory name", SUBCATEGORY_NAME_LIMIT,
            )
            prior = existing.get(name.casefold())
            if prior:
                if reactivate_existing and not prior["is_active"]:
                    connection.execute(
                        """UPDATE category_subcategories SET is_active=1,
                                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (prior["id"],),
                    )
                continue
            cursor = connection.execute(
                """INSERT INTO category_subcategories(category_id, name, sort_order)
                   VALUES (?, ?, ?)""",
                (category_id, name, next_order),
            )
            existing[name.casefold()] = {
                "id": int(cursor.lastrowid),
                "name": name,
                "is_active": 1,
            }
            next_order += 1

    def add_category(
        self, name: object, subcategories: list[str] | tuple[str, ...] = (),
    ) -> int:
        cleaned = self._clean_category_label(name, "Category name", CATEGORY_NAME_LIMIT)
        with self._transaction() as connection:
            self._unique_category_name(connection, cleaned)
            sort_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 value FROM categories"
            ).fetchone()["value"]
            cursor = connection.execute(
                "INSERT INTO categories(name, sort_order) VALUES (?, ?)",
                (cleaned, sort_order),
            )
            identifier = int(cursor.lastrowid)
            self._insert_subcategories(connection, identifier, subcategories)
        return identifier

    def replace_category_name(self, identifier: int, name: object) -> dict:
        """Rename unused definitions; preserve used history under an archived definition."""
        cleaned = self._clean_category_label(name, "Category name", CATEGORY_NAME_LIMIT)
        with self._transaction() as connection:
            source = self._category_by_id(connection, identifier)
            self._unique_category_name(connection, cleaned, excluding=identifier)
            if source["name"].casefold() == cleaned.casefold():
                if source["name"] != cleaned:
                    connection.execute(
                        "UPDATE categories SET name=? WHERE id=?", (cleaned, identifier),
                    )
                return {"id": identifier, "source_id": identifier, "history_preserved": False}
            linked = connection.execute(
                "SELECT COUNT(*) total FROM transactions WHERE category_id=?",
                (identifier,),
            ).fetchone()["total"]
            if linked == 0:
                connection.execute(
                    "UPDATE categories SET name=? WHERE id=?", (cleaned, identifier),
                )
                return {"id": identifier, "source_id": identifier, "history_preserved": False}

            cursor = connection.execute(
                """INSERT INTO categories(name, sort_order, is_active)
                   VALUES (?, ?, 1)""",
                (cleaned, source["sort_order"]),
            )
            target_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO category_subcategories(category_id, name, sort_order, is_active)
                   SELECT ?, name, sort_order, is_active FROM category_subcategories
                   WHERE category_id=?""",
                (target_id, identifier),
            )
            archived_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 value FROM categories"
            ).fetchone()["value"]
            connection.execute(
                "UPDATE categories SET is_active=0, sort_order=? WHERE id=?",
                (archived_order, identifier),
            )
        return {"id": target_id, "source_id": identifier, "history_preserved": True}

    def set_category_active(self, identifier: int, active: bool) -> None:
        with self._transaction() as connection:
            category = self._category_by_id(connection, identifier)
            if bool(category["is_active"]) == bool(active):
                return
            if not active:
                active_count = connection.execute(
                    "SELECT COUNT(*) total FROM categories WHERE is_active=1"
                ).fetchone()["total"]
                if active_count <= 1:
                    raise ValueError("Keep at least one active category")
            sort_order = category["sort_order"]
            if active:
                sort_order = connection.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 value FROM categories"
                ).fetchone()["value"]
            connection.execute(
                "UPDATE categories SET is_active=?, sort_order=? WHERE id=?",
                (int(active), sort_order, identifier),
            )

    def move_category(self, identifier: int, direction: int) -> None:
        if direction not in {-1, 1}:
            raise ValueError("Choose a valid category direction")
        with self._transaction() as connection:
            current = self._category_by_id(connection, identifier)
            if not current["is_active"]:
                raise ValueError("Restore the category before reordering it")
            identifiers = [row["id"] for row in connection.execute(
                "SELECT id FROM categories WHERE is_active=1 ORDER BY sort_order, id"
            )]
            position = identifiers.index(identifier)
            target = position + direction
            if target < 0 or target >= len(identifiers):
                return
            identifiers[position], identifiers[target] = identifiers[target], identifiers[position]
            connection.executemany(
                "UPDATE categories SET sort_order=? WHERE id=?",
                [(order, category_id) for order, category_id in enumerate(identifiers)],
            )

    def add_subcategory(self, category_id: int, name: object) -> None:
        with self._transaction() as connection:
            self._category_by_id(connection, category_id)
            cleaned = self._clean_category_label(
                name, "Subcategory name", SUBCATEGORY_NAME_LIMIT,
            )
            duplicate = connection.execute(
                """SELECT id, is_active FROM category_subcategories
                   WHERE category_id=? AND lower(name)=lower(?)""",
                (category_id, cleaned),
            ).fetchone()
            if duplicate:
                if duplicate["is_active"]:
                    raise ValueError("Use a unique subcategory name")
                connection.execute(
                    """UPDATE category_subcategories SET is_active=1,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (duplicate["id"],),
                )
                return
            self._insert_subcategories(connection, category_id, [cleaned])

    def set_subcategory_active(self, identifier: int, active: bool) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """UPDATE category_subcategories SET is_active=?,
                          updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (int(active), identifier),
            )
            self._require_record(cursor, "Subcategory no longer exists")

    def historical_subcategories(self, category_id: int) -> list[dict]:
        with self._connect() as connection:
            self._category_by_id(connection, category_id)
            rows = connection.execute(
                """SELECT label, SUM(transaction_count) transaction_count,
                          MAX(is_defined) is_defined, MAX(is_active) is_active,
                          MIN(sort_order) sort_order
                   FROM (
                       SELECT s.name label, 0 transaction_count, 1 is_defined,
                              s.is_active, s.sort_order
                       FROM category_subcategories s WHERE s.category_id=?
                       UNION ALL
                       SELECT trim(t.subcategory) label, COUNT(*) transaction_count,
                              0 is_defined, 0 is_active, 100000
                       FROM transactions t
                       WHERE t.category_id=? AND trim(t.subcategory)!=''
                       GROUP BY lower(trim(t.subcategory))
                   ) GROUP BY lower(label)
                   ORDER BY is_active DESC, sort_order, lower(label)""",
                (category_id, category_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def _category_migration_preview(
        cls,
        connection: Connection,
        source_category_id: int,
        target_category_id: int,
        source_subcategory: str | None,
        target_subcategory_action: str,
        target_subcategory: str | None,
    ) -> dict:
        if target_subcategory_action not in {"keep", "clear", "replace"}:
            raise ValueError("Choose what should happen to subcategories")
        source = cls._category_by_id(connection, source_category_id)
        target = cls._category_by_id(connection, target_category_id)
        if not target["is_active"]:
            raise ValueError("Choose an active target category")
        normalized_source = (
            cls._clean_category_label(
                source_subcategory, "Source subcategory", SUBCATEGORY_NAME_LIMIT,
            )
            if source_subcategory is not None else None
        )
        normalized_target = None
        if target_subcategory_action == "replace":
            normalized_target = cls._clean_category_label(
                target_subcategory, "Target subcategory", SUBCATEGORY_NAME_LIMIT,
            )
        transaction_clause = "category_id=?"
        transaction_parameters: list[object] = [source_category_id]
        mapping_clause = "category_id=?"
        mapping_parameters: list[object] = [source_category_id]
        if normalized_source is not None:
            transaction_clause += " AND lower(trim(subcategory))=lower(?)"
            transaction_parameters.append(normalized_source)
            mapping_clause += " AND lower(trim(subcategory))=lower(?)"
            mapping_parameters.append(normalized_source)
        transaction = connection.execute(
            f"""SELECT COUNT(*) transaction_count,
                       COALESCE(SUM(amount_cents), 0) amount_cents,
                       MIN(date) first_date, MAX(date) last_date
                FROM transactions WHERE {transaction_clause}""",
            transaction_parameters,
        ).fetchone()
        vendor_count = connection.execute(
            f"SELECT COUNT(*) total FROM vendor_mappings WHERE {mapping_clause}",
            mapping_parameters,
        ).fetchone()["total"]
        no_change = (
            source_category_id == target_category_id
            and target_subcategory_action == "keep"
        ) or (
            source_category_id == target_category_id
            and normalized_source is not None
            and target_subcategory_action == "replace"
            and normalized_source.casefold() == normalized_target.casefold()
        )
        return {
            "source_category_id": source_category_id,
            "source_category": source["name"],
            "source_subcategory": normalized_source,
            "target_category_id": target_category_id,
            "target_category": target["name"],
            "target_subcategory_action": target_subcategory_action,
            "target_subcategory": normalized_target,
            "transaction_count": transaction["transaction_count"],
            "amount_cents": transaction["amount_cents"],
            "first_date": transaction["first_date"],
            "last_date": transaction["last_date"],
            "vendor_mapping_count": vendor_count,
            "no_change": no_change,
        }

    def category_migration_preview(
        self,
        source_category_id: int,
        target_category_id: int,
        *,
        source_subcategory: str | None = None,
        target_subcategory_action: str = "keep",
        target_subcategory: str | None = None,
    ) -> dict:
        with self._connect() as connection:
            return self._category_migration_preview(
                connection, source_category_id, target_category_id,
                source_subcategory, target_subcategory_action, target_subcategory,
            )

    def apply_category_migration(
        self,
        source_category_id: int,
        target_category_id: int,
        *,
        source_subcategory: str | None = None,
        target_subcategory_action: str = "keep",
        target_subcategory: str | None = None,
    ) -> dict:
        """Apply one explicit historical mapping and retain a concise audit record."""
        with self._transaction() as connection:
            preview = self._category_migration_preview(
                connection, source_category_id, target_category_id,
                source_subcategory, target_subcategory_action, target_subcategory,
            )
            if preview["no_change"]:
                raise ValueError("Choose a mapping that changes the historical records")
            if preview["transaction_count"] == 0 and preview["vendor_mapping_count"] == 0:
                raise ValueError("No historical records match this mapping")

            source_clause = "category_id=?"
            source_parameters: list[object] = [source_category_id]
            if preview["source_subcategory"] is not None:
                source_clause += " AND lower(trim(subcategory))=lower(?)"
                source_parameters.append(preview["source_subcategory"])

            vendor_rows = connection.execute(
                f"""SELECT id, bank, vendor_key, description, subcategory, score, uses
                     FROM vendor_mappings WHERE {source_clause}""",
                source_parameters,
            ).fetchall()
            if vendor_rows:
                connection.executemany(
                    "DELETE FROM vendor_mappings WHERE id=?",
                    [(row["id"],) for row in vendor_rows],
                )

            if target_subcategory_action == "keep":
                connection.execute(
                    f"""UPDATE transactions SET category_id=?, updated_at=CURRENT_TIMESTAMP
                         WHERE {source_clause}""",
                    (target_category_id, *source_parameters),
                )
            else:
                replacement = preview["target_subcategory"] or ""
                connection.execute(
                    f"""UPDATE transactions SET category_id=?, subcategory=?,
                                updated_at=CURRENT_TIMESTAMP WHERE {source_clause}""",
                    (target_category_id, replacement, *source_parameters),
                )

            for row in vendor_rows:
                replacement = (
                    row["subcategory"] if target_subcategory_action == "keep"
                    else preview["target_subcategory"] or ""
                )
                connection.execute(
                    """INSERT INTO vendor_mappings
                       (bank, vendor_key, description, category_id, subcategory, score, uses)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bank, vendor_key, description, category_id, subcategory)
                       DO UPDATE SET score=vendor_mappings.score + excluded.score,
                                     uses=vendor_mappings.uses + excluded.uses,
                                     updated_at=CURRENT_TIMESTAMP""",
                    (
                        row["bank"], row["vendor_key"], row["description"],
                        target_category_id, replacement, row["score"], row["uses"],
                    ),
                )

            if preview["target_subcategory"]:
                self._insert_subcategories(
                    connection, target_category_id, [preview["target_subcategory"]],
                    reactivate_existing=True,
                )
            cursor = connection.execute(
                """INSERT INTO category_migration_log
                   (source_category, source_subcategory, target_category,
                    target_subcategory_action, target_subcategory,
                    affected_transactions, affected_vendor_mappings,
                    first_transaction_date, last_transaction_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    preview["source_category"], preview["source_subcategory"],
                    preview["target_category"], preview["target_subcategory_action"],
                    preview["target_subcategory"], preview["transaction_count"],
                    preview["vendor_mapping_count"], preview["first_date"],
                    preview["last_date"],
                ),
            )
            preview["migration_id"] = int(cursor.lastrowid)
        return preview

    def category_migration_history(self, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM category_migration_log
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_manual_date(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT date FROM transactions WHERE source_key IS NULL
                   ORDER BY created_at DESC, id DESC LIMIT 1"""
            ).fetchone()
        return row["date"] if row else None

    def last_manual_account_id(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT t.account_id FROM transactions t
                   JOIN accounts a ON a.id=t.account_id AND a.is_active=1
                   WHERE t.source_key IS NULL
                   ORDER BY t.created_at DESC, t.id DESC LIMIT 1"""
            ).fetchone()
        return int(row["account_id"]) if row else None

    def vendor_preferences(self, bank: str, vendor_keys: list[str]) -> dict[str, dict]:
        """Return each vendor's bank-specific choice, then its best cross-bank choice."""
        keys = list(dict.fromkeys(key for key in vendor_keys if key))
        if not keys:
            return {}
        rows = []
        with self._connect() as connection:
            for key_chunk in _chunks(keys):
                placeholders = ",".join("?" for _ in key_chunk)
                rows.extend(connection.execute(
                    f"""SELECT vm.vendor_key, vm.description, c.name category,
                               vm.subcategory, vm.score, vm.uses, vm.updated_at,
                               CASE WHEN vm.bank=? THEN 0 ELSE 1 END bank_rank
                        FROM vendor_mappings vm JOIN categories c ON c.id=vm.category_id
                        WHERE vm.vendor_key IN ({placeholders})
                          AND c.is_active=1
                        ORDER BY vm.vendor_key, bank_rank, vm.score DESC, vm.uses DESC,
                                 vm.updated_at DESC, vm.id DESC""",
                    (bank, *key_chunk),
                ).fetchall())
        result: dict[str, dict] = {}
        for row in rows:
            result.setdefault(row["vendor_key"], dict(row))
        return result

    def description_catalog(self) -> list[dict]:
        """Load the compact, history-derived autocomplete source once."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT lower(trim(t.description)) normalized,
                          MAX(t.description) description, c.name category,
                          t.subcategory, COUNT(*) uses, MAX(t.date) last_used
                   FROM transactions t JOIN categories c ON c.id=t.category_id
                   WHERE c.is_active=1
                   GROUP BY lower(trim(t.description)), c.name, t.subcategory"""
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def description_assistance(
        catalog: list[dict], prefix: str, limit: int = 6,
    ) -> tuple[list[dict], dict | None]:
        """Match and predict from an in-memory catalog without another SQLCipher open."""
        normalized = normalize_description(prefix)
        if not normalized:
            return [], None

        matches = [row for row in catalog if normalized in row["normalized"]]
        matches.sort(key=lambda row: (row["last_used"], row["description"]), reverse=True)
        matches.sort(key=lambda row: row["uses"], reverse=True)
        matches.sort(key=lambda row: not row["normalized"].startswith(normalized))

        exact = [row for row in catalog if row["normalized"] == normalized]
        predicted = None
        if exact:
            categories: dict[str, dict] = {}
            for row in exact:
                aggregate = categories.setdefault(
                    row["category"], {"uses": 0, "last_used": "", "rows": []},
                )
                aggregate["uses"] += row["uses"]
                aggregate["last_used"] = max(aggregate["last_used"], row["last_used"])
                aggregate["rows"].append(row)
            category, aggregate = min(
                categories.items(),
                key=lambda item: (-item[1]["uses"], -int(item[1]["last_used"].replace("-", "")), item[0]),
            )
            subcategory_rows = aggregate["rows"]
            subcategory_rows.sort(
                key=lambda row: (-row["uses"], -int(row["last_used"].replace("-", "")), row["subcategory"]),
            )
            predicted = {
                "category": category,
                "subcategory": subcategory_rows[0]["subcategory"] if subcategory_rows else "",
            }
        return matches[:limit], predicted

    def import_duplicate_snapshot(
        self,
        new_source_keys: list[str],
        legacy_source_keys: list[str],
        transaction_dates: list[date],
        amount_cents: list[int],
        account_id: int | None,
    ) -> dict:
        """Fetch exact and semantic duplicate candidates for a whole review batch."""
        source_keys = list(dict.fromkeys([*new_source_keys, *legacy_source_keys]))
        dates = sorted(set(transaction_dates))
        amounts = sorted(set(amount_cents))
        existing_sources: set[str] = set()
        semantic_vendor: set[tuple[str, int, str]] = set()
        semantic_description: set[tuple[str, int, str]] = set()
        with self._connect() as connection:
            if source_keys:
                source_rows = []
                for key_chunk in _chunks(source_keys):
                    placeholders = ",".join("?" for _ in key_chunk)
                    source_rows.extend(connection.execute(
                        f"SELECT source_key, account_id FROM transactions "
                        f"WHERE source_key IN ({placeholders})",
                        key_chunk,
                    ).fetchall())
                new_keys = set(new_source_keys)
                legacy_keys = set(legacy_source_keys)
                for row in source_rows:
                    key = row["source_key"]
                    if key in new_keys or (key in legacy_keys and row["account_id"] == account_id):
                        existing_sources.add(key)
            if dates and amounts:
                rows = []
                for amount_chunk in _chunks(amounts):
                    amount_placeholders = ",".join("?" for _ in amount_chunk)
                    rows.extend(connection.execute(
                        f"""SELECT date, amount_cents, source_vendor_key, description
                             FROM transactions
                             WHERE account_id IS ? AND date>=? AND date<=?
                               AND amount_cents IN ({amount_placeholders})""",
                        (
                            account_id, dates[0].isoformat(), dates[-1].isoformat(),
                            *amount_chunk,
                        ),
                    ).fetchall())
                for row in rows:
                    key = (row["date"], row["amount_cents"])
                    if row["source_vendor_key"]:
                        semantic_vendor.add((*key, row["source_vendor_key"]))
                    else:
                        semantic_description.add(
                            (*key, normalize_description(row["description"])),
                        )
        return {
            "sources": existing_sources,
            "semantic_vendor": semantic_vendor,
            "semantic_description": semantic_description,
        }

    @staticmethod
    def month_window(end_month: str, count: int) -> list[str]:
        validate_month(end_month)
        if count < 1:
            raise ValueError("Month window must contain at least one month")
        return [shifted_month(end_month, -offset) for offset in range(count - 1, -1, -1)]

    def add(self, item: TransactionInput) -> int:
        amount_cents, spread_months = self._prepare_transaction(item)
        with self._transaction() as connection:
            category_id = self._category_id(connection, item.category)
            self._validate_account_id(connection, item.account_id)
            if item.subcategory.strip():
                self._insert_subcategories(connection, category_id, [item.subcategory])
            cursor = self._insert_transaction(
                connection, item, amount_cents, category_id, spread_months,
            )
            identifier = int(cursor.lastrowid)
        return identifier

    def add_bank_import(
        self, items: list[TransactionInput], metadata: BankImportMetadata,
    ) -> int:
        """Atomically commit a reviewed bank batch and its non-sensitive provenance."""
        if not items:
            raise ValueError("Select at least one transaction to import")
        filename = metadata.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        bank = metadata.bank.strip()
        if (
            not filename
            or len(filename) > 255
            or any(ord(character) < 32 for character in filename)
        ):
            raise ValueError("Use a valid import filename")
        if not bank:
            raise ValueError("Bank imports require a bank name")
        if metadata.first_transaction_date > metadata.last_transaction_date:
            raise ValueError("Import transaction dates are out of order")
        if metadata.selected_row_count != len(items):
            raise ValueError("Selected import row count does not match the reviewed rows")
        if metadata.source_row_count < metadata.selected_row_count:
            raise ValueError("Source row count cannot be smaller than the selected row count")
        if any(item.source_bank.strip() != bank for item in items):
            raise ValueError("Reviewed transactions must match the selected bank")
        if any(item.account_id != metadata.account_id for item in items):
            raise ValueError("Reviewed transactions must match the selected account")
        item_dates = [item.date for item in items]
        if (
            min(item_dates) < metadata.first_transaction_date
            or max(item_dates) > metadata.last_transaction_date
        ):
            raise ValueError("Import date range does not cover every selected transaction")

        prepared: list[tuple[TransactionInput, int, int]] = []
        for item in items:
            if not item.source_key or not item.source_bank or not item.source_vendor_key:
                raise ValueError("Bank imports require stable source metadata")
            if not item.description.strip():
                raise ValueError("Every selected row needs a description")
            amount_cents, spread_months = self._prepare_transaction(item)
            prepared.append((item, amount_cents, spread_months))

        inserted = 0
        with self._transaction() as connection:
            categories = {
                row["name"]: row["id"] for row in connection.execute(
                    "SELECT id, name FROM categories WHERE is_active=1"
                )
            }
            missing = sorted({item.category for item, _, _ in prepared} - categories.keys())
            if missing:
                raise ValueError(f"Unknown category: {', '.join(missing)}")

            self._validate_account_id(connection, metadata.account_id)
            source_keys = [item.source_key for item, _, _ in prepared]
            existing = set()
            for key_chunk in _chunks(source_keys):
                placeholders = ",".join("?" for _ in key_chunk)
                existing.update(
                    row["source_key"] for row in connection.execute(
                        f"SELECT source_key FROM transactions "
                        f"WHERE source_key IN ({placeholders})",
                        key_chunk,
                    )
                )

            pending = []
            for entry in prepared:
                item = entry[0]
                if item.source_key in existing:
                    continue
                pending.append(entry)
                existing.add(item.source_key)

            subcategories: dict[str, list[str]] = defaultdict(list)
            for item, _, _ in pending:
                if item.subcategory.strip():
                    subcategories[item.category].append(item.subcategory)
            for category, names in subcategories.items():
                self._insert_subcategories(connection, categories[category], names)

            for item, amount_cents, spread_months in pending:
                self._insert_transaction(
                    connection, item, amount_cents, categories[item.category], spread_months,
                )
                self._learn_vendor_mapping(
                    connection,
                    bank=bank,
                    vendor_key=item.source_vendor_key,
                    description=item.description,
                    category_id=categories[item.category],
                    subcategory=item.subcategory,
                )
                inserted += 1

            connection.execute(
                """INSERT INTO bank_import_history
                   (filename, bank, account_id, first_transaction_date,
                    last_transaction_date, source_row_count, selected_row_count,
                    imported_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    filename, bank, metadata.account_id,
                    metadata.first_transaction_date.isoformat(),
                    metadata.last_transaction_date.isoformat(),
                    metadata.source_row_count, metadata.selected_row_count, inserted,
                ),
            )
        return inserted

    def recent_bank_imports(self, limit: int = 6) -> list[dict]:
        """Return recent import metadata without retaining or reconstructing CSV data."""
        if limit < 1:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT h.*, a.name account_name,
                          a.last_four account_last_four
                   FROM bank_import_history h
                   LEFT JOIN accounts a ON a.id=h.account_id
                   ORDER BY h.created_at DESC, h.id DESC LIMIT ?""",
                (min(limit, 50),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _learn_vendor_mapping(
        connection: Connection,
        *,
        bank: str,
        vendor_key: str,
        description: str,
        category_id: int,
        subcategory: str,
    ) -> None:
        connection.execute(
            "UPDATE vendor_mappings SET score=score * 0.85 "
            "WHERE bank=? AND vendor_key=?",
            (bank, vendor_key),
        )
        connection.execute(
            """INSERT INTO vendor_mappings
               (bank, vendor_key, description, category_id, subcategory, score, uses)
               VALUES (?, ?, ?, ?, ?, 1.0, 1)
               ON CONFLICT(bank, vendor_key, description, category_id, subcategory)
               DO UPDATE SET score=vendor_mappings.score + 1.0,
                             uses=vendor_mappings.uses + 1,
                             updated_at=CURRENT_TIMESTAMP""",
            (bank, vendor_key, description.strip(), category_id, subcategory.strip()),
        )

    def update(self, identifier: int, item: TransactionInput) -> None:
        amount_cents, spread_months = self._prepare_transaction(item)
        with self._transaction() as connection:
            category_id = self._category_id(connection, item.category)
            self._validate_account_id(connection, item.account_id)
            existing = connection.execute(
                """SELECT source_bank, source_vendor_key, description, category_id, subcategory
                   FROM transactions WHERE id=?""",
                (identifier,),
            ).fetchone()
            if existing is None:
                raise ValueError("Expense no longer exists")
            if item.subcategory.strip():
                self._insert_subcategories(connection, category_id, [item.subcategory])
            cursor = connection.execute(
                """UPDATE transactions SET date=?, amount_cents=?, transaction_kind=?,
                   description=?, category_id=?,
                   subcategory=?, purpose=?, expense_type=?, need_want=?, notes=?, spread_months=?,
                   account_id=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                self._transaction_values(
                    item, amount_cents, category_id, spread_months,
                ) + (identifier,),
            )
            self._require_record(cursor, "Expense no longer exists")
            new_choice = (item.description.strip(), category_id, item.subcategory.strip())
            old_choice = (
                existing["description"], existing["category_id"], existing["subcategory"],
            )
            if (
                existing["source_bank"]
                and existing["source_vendor_key"]
                and new_choice != old_choice
            ):
                self._learn_vendor_mapping(
                    connection,
                    bank=existing["source_bank"],
                    vendor_key=existing["source_vendor_key"],
                    description=item.description,
                    category_id=category_id,
                    subcategory=item.subcategory,
                )

    @classmethod
    def _prepare_transaction(cls, item: TransactionInput) -> tuple[int, int]:
        return (
            parse_transaction_amount(item.amount, item.transaction_kind),
            cls._validated_spread(item),
        )

    @staticmethod
    def _category_id(connection: Connection, category: str) -> int:
        row = connection.execute(
            "SELECT id FROM categories WHERE name = ?", (category,),
        ).fetchone()
        if not row:
            raise ValueError("Choose a category")
        return int(row["id"])

    @staticmethod
    def _validate_account_id(connection: Connection, account_id: int | None) -> None:
        if account_id is None:
            return
        if not connection.execute(
            "SELECT 1 FROM accounts WHERE id=?", (account_id,),
        ).fetchone():
            raise ValueError("Choose a valid account or leave it unassigned")

    @staticmethod
    def _validated_spread(item: TransactionInput) -> int:
        return ANNUAL_EXPENSE_MONTHS if item.expense_type == ANNUAL_EXPENSE_TYPE else 1

    @staticmethod
    def _transaction_values(
        item: TransactionInput, amount_cents: int, category_id: int, spread_months: int,
    ) -> tuple[object, ...]:
        return (
            item.date.isoformat(), amount_cents, item.transaction_kind,
            item.description.strip(), category_id, item.subcategory.strip(),
            item.purpose.strip(), item.expense_type, item.need_want,
            item.notes.strip(), spread_months, item.account_id,
        )

    @classmethod
    def _insert_transaction(
        cls, connection: Connection, item: TransactionInput, amount_cents: int,
        category_id: int, spread_months: int,
    ) -> Cursor:
        values = cls._transaction_values(item, amount_cents, category_id, spread_months)
        source_values = (
            item.source_key, item.source_bank.strip(), item.source_vendor.strip(),
            item.source_vendor_key.strip(),
        )
        return connection.execute(TRANSACTION_INSERT_SQL, values + source_values)

    def delete(self, identifier: int) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM transactions WHERE id = ?", (identifier,)
            )
            self._require_record(cursor, "Expense no longer exists")

    def add_income(self, item: IncomeInput) -> int:
        amount_cents = parse_amount(item.amount)
        if not item.description.strip():
            raise ValueError("Add an income description")
        with self._transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO income_entries(date, amount_cents, description, notes)
                   VALUES (?, ?, ?, ?)""",
                (item.date.isoformat(), amount_cents, item.description.strip(), item.notes.strip()),
            )
            identifier = int(cursor.lastrowid)
        return identifier

    def income_estimate(self, month: str) -> dict:
        """Return a user override or an auditable forecast using only prior months."""
        validate_month(month)
        history_end, _ = month_bounds(month)
        with self._connect() as connection:
            override = connection.execute(
                "SELECT amount_cents, updated_at FROM income_estimates WHERE month=?",
                (month,),
            ).fetchone()
            history = connection.execute(
                """SELECT substr(date, 1, 7) month, SUM(amount_cents) total
                   FROM income_entries WHERE date < ?
                   GROUP BY substr(date, 1, 7) ORDER BY month DESC LIMIT ?""",
                (history_end, INCOME_FORECAST_MONTHS),
            ).fetchall()
        forecast = weighted_income_forecast(
            [(row["month"], row["total"]) for row in reversed(history)],
            target_month=month,
        )
        return {
            "month": month,
            "amount_cents": override["amount_cents"] if override else forecast.amount_cents,
            "is_override": override is not None,
            "calculated_cents": forecast.amount_cents,
            "method": forecast.method,
            "observations": forecast.observations,
            "source_months": forecast.source_months,
        }

    def save_income_estimate(self, item: IncomeEstimateInput) -> None:
        validate_month(item.month)
        amount_cents = parse_nonnegative_amount(item.amount, "Estimated income")
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO income_estimates(month, amount_cents) VALUES (?, ?)
                   ON CONFLICT(month) DO UPDATE SET amount_cents=excluded.amount_cents,
                       updated_at=CURRENT_TIMESTAMP""",
                (item.month, amount_cents),
            )

    def clear_income_estimate(self, month: str) -> None:
        validate_month(month)
        with self._transaction() as connection:
            connection.execute("DELETE FROM income_estimates WHERE month=?", (month,))

    def budgets(self, month: str, *, category_trend: dict | None = None) -> list[dict]:
        if self._shared_connection is None and category_trend is None:
            with self.read_session() as reader:
                return reader.budgets(month)
        validate_month(month)
        trend = category_trend or self.category_trend(month, count=1)
        actuals = {series["name"]: series["values"][-1] for series in trend["series"]}
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT c.name category, c.sort_order, c.is_active,
                          COALESCE((SELECT b.amount_cents FROM monthly_budgets b
                                    WHERE b.category_id=c.id AND b.month<=?
                                    ORDER BY b.month DESC LIMIT 1), 0) amount_cents,
                          (SELECT b.month FROM monthly_budgets b
                           WHERE b.category_id=c.id AND b.month<=?
                           ORDER BY b.month DESC LIMIT 1) effective_month
                   FROM categories c WHERE c.is_active=1 ORDER BY c.sort_order""",
                (month, month),
            ).fetchall()
        return [
            {
                **dict(row),
                "actual_cents": actuals.get(row["category"], 0),
                "remaining_cents": row["amount_cents"] - actuals.get(row["category"], 0),
            }
            for row in rows
        ]

    def budget_plan_info(self, month: str) -> dict:
        validate_month(month)
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) total FROM monthly_budgets").fetchone()["total"]
            revision = connection.execute(
                "SELECT MAX(month) month FROM monthly_budgets WHERE month<=?", (month,)
            ).fetchone()["month"]
        return {
            "has_budget": count > 0,
            "effective_month": revision,
            "is_default": revision == "0001-01",
        }

    def budget_trend(
        self, end_month: str, *, count: int = 12, category: str | None = None,
    ) -> dict:
        """Return the effective auditable budget at each visible month."""
        months = self.month_window(end_month, count)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT c.name category, b.month, b.amount_cents
                   FROM monthly_budgets b JOIN categories c ON c.id=b.category_id
                   WHERE c.is_active=1 AND b.month<=? AND (? IS NULL OR c.name=?)
                   ORDER BY c.sort_order, c.id, b.month""",
                (end_month, category, category),
            ).fetchall()
        revisions: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for row in rows:
            revisions[row["category"]].append((row["month"], row["amount_cents"]))
        values: list[int | None] = []
        for month in months:
            effective = [
                next(
                    (amount for revision, amount in reversed(items) if revision <= month),
                    None,
                )
                for items in revisions.values()
            ]
            configured = [amount for amount in effective if amount is not None]
            values.append(sum(configured) if configured else None)
        return {"months": months, "values": values}

    def total_monthly_budget(self) -> int | None:
        """Return the optional overall monthly limit explicitly set by the user."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key=?",
                (TOTAL_MONTHLY_BUDGET_KEY,),
            ).fetchone()
        return int(row["value"]) if row is not None else None

    def total_budget_trend(self, end_month: str, *, count: int = 12) -> dict:
        """Repeat the current overall limit across the visible month window."""
        months = self.month_window(end_month, count)
        amount = self.total_monthly_budget()
        return {
            "months": months,
            "values": [amount if amount and amount > 0 else None for _ in months],
        }

    @staticmethod
    def _budget_values_at(connection: Connection, month: str) -> dict[int, int]:
        return {
            row["category_id"]: row["amount_cents"]
            for row in connection.execute(
                """SELECT c.id category_id,
                          COALESCE((SELECT b.amount_cents FROM monthly_budgets b
                                    WHERE b.category_id=c.id AND b.month<=?
                                    ORDER BY b.month DESC LIMIT 1), 0) amount_cents
                   FROM categories c""",
                (month,),
            )
        }

    def save_budgets(
        self,
        month: str,
        amounts: dict[str, object],
        scope: str = "from_month",
        *,
        total_amount: object = _UNCHANGED,
    ) -> None:
        validate_month(month)
        if scope not in {"from_month", "all_time", "year"}:
            raise ValueError("Choose a valid budget scope")
        prepared = {
            category: parse_nonnegative_amount(value or "0", f"{category} budget")
            for category, value in amounts.items()
        }
        prepared_total = _UNCHANGED
        if total_amount is not _UNCHANGED:
            raw_total = str(total_amount or "").strip()
            prepared_total = (
                parse_nonnegative_amount(raw_total, "Total monthly limit")
                if raw_total else None
            )
        with self._transaction() as connection:
            known = {
                row["name"]: row["id"] for row in connection.execute(
                    "SELECT id, name FROM categories"
                )
            }
            unknown = sorted(set(prepared) - set(known))
            if unknown:
                raise ValueError(f"Unknown category: {', '.join(unknown)}")
            has_budget = connection.execute(
                "SELECT 1 FROM monthly_budgets LIMIT 1"
            ).fetchone() is not None
            effective_month = month
            restore_month = restore_values = None
            replacing_all_time = not has_budget or scope == "all_time"
            if replacing_all_time:
                connection.execute("DELETE FROM monthly_budgets")
                effective_month = "0001-01"
            elif scope == "year":
                year = month[:4]
                restore_month = f"{int(year) + 1:04d}-01"
                restore_values = self._budget_values_at(connection, restore_month)
                connection.execute(
                    "DELETE FROM monthly_budgets WHERE month>=? AND month<?",
                    (f"{year}-01", restore_month),
                )
                effective_month = f"{year}-01"
                connection.execute("DELETE FROM monthly_budgets WHERE month=?", (restore_month,))
            else:
                connection.execute("DELETE FROM monthly_budgets WHERE month=?", (month,))
            connection.executemany(
                """INSERT INTO monthly_budgets(month, category_id, amount_cents)
                   VALUES (?, ?, ?)""",
                [
                    (effective_month, known[category], cents)
                    for category, cents in prepared.items()
                    if cents > 0 or not replacing_all_time
                ],
            )
            if restore_month and restore_values is not None:
                connection.executemany(
                    """INSERT INTO monthly_budgets(month, category_id, amount_cents)
                       VALUES (?, ?, ?)""",
                    [
                        (restore_month, category_id, cents)
                        for category_id, cents in restore_values.items()
                    ],
                )
            if prepared_total is not _UNCHANGED:
                if prepared_total:
                    connection.execute(
                        """INSERT INTO app_settings(key, value) VALUES (?, ?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                        (TOTAL_MONTHLY_BUDGET_KEY, str(prepared_total)),
                    )
                else:
                    connection.execute(
                        "DELETE FROM app_settings WHERE key=?",
                        (TOTAL_MONTHLY_BUDGET_KEY,),
                    )

    def save_liability(self, item: LiabilityInput, identifier: int | None = None) -> int:
        name = item.name.strip()
        if not name:
            raise ValueError("Name is required")
        if item.liability_type not in LIABILITY_TYPES:
            raise ValueError("Choose a loan type")
        principal_cents = parse_amount(item.original_principal)
        try:
            rate = Decimal(str(item.annual_rate_percent))
        except InvalidOperation:
            raise ValueError("Enter a valid interest rate") from None
        if not rate.is_finite() or rate < 0 or rate > 100:
            raise ValueError("Interest rate must be between 0% and 100%")
        annual_rate_bps = int((rate * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        term_months = int(item.term_months)
        if item.rate_type not in MORTGAGE_RATE_TYPES:
            raise ValueError("Choose a valid rate type")
        if item.interest_convention not in INTEREST_CONVENTIONS:
            raise ValueError("Choose a valid interest convention")
        if item.payment_frequency not in PAYMENT_FREQUENCIES:
            raise ValueError("Choose a valid payment frequency")
        rate_term_months = int(item.rate_term_months)
        if rate_term_months < 1 or rate_term_months > term_months:
            raise ValueError("Mortgage term must be between one month and the amortization")
        current_balance_cents = (
            principal_cents
            if item.current_balance is None
            else parse_amount(item.current_balance)
        )
        balance_as_of = item.balance_as_of or (item.start_date - timedelta(days=1))
        payment_cents = (
            parse_amount(item.payment_amount)
            if item.payment_amount is not None
            else loan_payment_cents(
                current_balance_cents, annual_rate_bps, term_months,
                item.interest_convention, item.payment_frequency,
            )
        )
        match_key = item.payment_match_key.strip()
        if match_key and not match_key.startswith(("vendor:", "description:")):
            raise ValueError("Choose a valid payment transaction")
        values = (
            name, item.liability_type, principal_cents, annual_rate_bps,
            term_months, item.start_date.isoformat(), payment_cents, item.notes.strip(),
            match_key, item.payment_match_label.strip(),
            item.rate_type, item.interest_convention, rate_term_months,
            current_balance_cents, balance_as_of.isoformat(), item.payment_frequency,
        )
        with self._transaction() as connection:
            if identifier is None:
                cursor = connection.execute(
                    """INSERT INTO liabilities
                       (name, liability_type, original_principal_cents, annual_rate_bps,
                        term_months, start_date, payment_cents, notes,
                        payment_match_key, payment_match_label, rate_type,
                        interest_convention, rate_term_months, current_balance_cents,
                        balance_as_of_date, payment_frequency)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                identifier = int(cursor.lastrowid)
            else:
                cursor = connection.execute(
                    """UPDATE liabilities SET name=?, liability_type=?,
                       original_principal_cents=?, annual_rate_bps=?, term_months=?,
                       start_date=?, payment_cents=?, notes=?, payment_match_key=?,
                        payment_match_label=?, rate_type=?, interest_convention=?,
                        rate_term_months=?, current_balance_cents=?, balance_as_of_date=?,
                        payment_frequency=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    values + (identifier,),
                )
                self._require_record(cursor, "Loan no longer exists")
        return identifier

    def recurring_payment_candidates(self, limit: int = 40) -> list[dict]:
        """Return deterministic recurring debit series suitable for explicit linking."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT
                       CASE WHEN source_vendor_key!='' THEN 'vendor:' || source_vendor_key
                            ELSE 'description:' || lower(trim(description)) END match_key,
                       MAX(CASE WHEN source_vendor_key!='' AND source_vendor!='' THEN source_vendor
                                ELSE description END) label,
                       COUNT(*) uses, CAST(ROUND(AVG(amount_cents)) AS INTEGER) average_cents,
                       MAX(date) last_date
                   FROM transactions WHERE amount_cents>0
                   GROUP BY match_key HAVING COUNT(*)>=2
                   ORDER BY uses DESC, last_date DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _matched_payment_history(
        connection: Connection, match_key: str, start: date, as_of: date,
    ) -> dict[str, int]:
        if not match_key:
            return {}
        prefix, value = match_key.split(":", 1)
        clause = "source_vendor_key=?" if prefix == "vendor" else "lower(trim(description))=?"
        return {
            row["month"]: row["total"]
            for row in connection.execute(
                f"""SELECT substr(date, 1, 7) month, SUM(amount_cents) total
                    FROM transactions WHERE amount_cents>0 AND {clause}
                       AND date>? AND date<=? GROUP BY substr(date, 1, 7)
                    ORDER BY month""",
                (value, start.isoformat(), as_of.isoformat()),
            )
        }

    @staticmethod
    def _scheduled_payment_months(
        first_payment: date, start_exclusive: date, end_inclusive: date,
    ) -> list[str]:
        if end_inclusive <= start_exclusive:
            return []
        cursor = start_exclusive.replace(day=1)
        end_month = end_inclusive.replace(day=1)
        months: list[str] = []
        while cursor <= end_month:
            month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
            period_end = min(month_end, end_inclusive)
            period_start = max(start_exclusive, cursor - timedelta(days=1))
            if (
                scheduled_payments_due(first_payment, period_end)
                > scheduled_payments_due(first_payment, period_start)
            ):
                months.append(cursor.strftime("%Y-%m"))
            cursor = date.fromisoformat(shifted_month(cursor.strftime("%Y-%m"), 1) + "-01")
        return months

    def liabilities(self, as_of: date | None = None) -> list[dict]:
        as_of = as_of or date.today()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM liabilities ORDER BY start_date, id"
            ).fetchall()
            results = []
            for row in rows:
                record = dict(row)
                anchor = date.fromisoformat(record["balance_as_of_date"])
                if as_of < anchor:
                    record.update({
                        "payments_made": 0,
                        "scheduled_balance_cents": None,
                        "estimated_balance_cents": None,
                        "observed_payment_cents": None,
                        "observed_months": 0,
                        "contractual_monthly_cents": payment_monthly_equivalent(
                            record["payment_cents"], record["payment_frequency"],
                        ),
                        "projected_payment_cents": 0,
                        "projected_payoff_months": None,
                        "rate_term_remaining_months": record["rate_term_months"],
                        "rate_term_balance_cents": None,
                        "payment_history": {},
                    })
                    results.append(record)
                    continue
                payment_months = self._scheduled_payment_months(
                    date.fromisoformat(record["start_date"]), anchor, as_of,
                )[:record["term_months"]]
                payments = len(payment_months)
                history = self._matched_payment_history(
                    connection, record["payment_match_key"], anchor, as_of,
                )
                contractual_monthly = payment_monthly_equivalent(
                    record["payment_cents"], record["payment_frequency"],
                )
                applied = []
                for payment_month in payment_months:
                    applied.append(history.get(payment_month, contractual_monthly))
                observed_values = [history[month] for month in sorted(history)][-6:]
                observed_average = exponential_average_cents(observed_values)
                scheduled_balance = balance_after_payments_cents(
                    record["current_balance_cents"], record["annual_rate_bps"],
                    [contractual_monthly] * payments, record["interest_convention"],
                )
                adjusted_balance = balance_after_payments_cents(
                    record["current_balance_cents"], record["annual_rate_bps"], applied,
                    record["interest_convention"],
                )
                projection_payment = observed_average or contractual_monthly
                rate_term_remaining = max(0, record["rate_term_months"] - payments)
                record.update({
                    "payments_made": payments,
                    "scheduled_balance_cents": scheduled_balance,
                    "estimated_balance_cents": adjusted_balance,
                    "observed_payment_cents": observed_average,
                    "observed_months": len(history),
                    "contractual_monthly_cents": contractual_monthly,
                    "projected_payment_cents": projection_payment,
                    "projected_payoff_months": projected_payoff_months(
                        adjusted_balance, record["annual_rate_bps"], projection_payment,
                        record["interest_convention"],
                    ),
                    "rate_term_remaining_months": rate_term_remaining,
                    "rate_term_balance_cents": balance_after_payments_cents(
                        adjusted_balance, record["annual_rate_bps"],
                        [projection_payment] * rate_term_remaining,
                        record["interest_convention"],
                    ),
                    "payment_history": history,
                })
                results.append(record)
        return results

    def liability_insights(self, end_month: str, count: int = 12) -> dict:
        """Return auditable debt balances and payment history for Insights.

        Balance points reuse :meth:`liabilities`, so the Liabilities and
        Insights pages cannot drift into separate amortization calculations.
        Imported matched payments are observed values; months without a match
        use the stored contractual payment and are kept in a separate series.
        """
        if self._shared_connection is None:
            with self.read_session() as reader:
                return reader.liability_insights(end_month, count)
        validate_month(end_month)
        if count < 2:
            raise ValueError("Liability insights need at least two months")

        today = date.today()
        effective_end_month = min(end_month, today.strftime("%Y-%m"))
        months = self.month_window(effective_end_month, count)

        def month_end(month: str) -> date:
            _, following = month_bounds(month)
            return date.fromisoformat(following) - timedelta(days=1)

        as_of = min(month_end(effective_end_month), today)
        current = [
            item for item in self.liabilities(as_of=as_of)
            if date.fromisoformat(item["balance_as_of_date"]) <= as_of
        ]
        if not current:
            return {
                "as_of": as_of.isoformat(), "months": months, "loans": [],
                "total_original_cents": 0, "total_balance_cents": 0,
                "total_repaid_cents": 0, "monthly_payment_cents": 0,
                "paydown_pace_cents": 0, "projected_payoff_months": None,
                "balance_series": [], "observed_payments": [0] * count,
                "scheduled_payments": [0] * count,
            }

        current_by_id = {item["id"]: item for item in current}
        balance_by_id = {identifier: [] for identifier in current_by_id}
        total_balances: list[int | None] = []
        for month in months:
            point_as_of = min(month_end(month), as_of)
            point = {
                item["id"]: item for item in self.liabilities(as_of=point_as_of)
                if item["id"] in current_by_id
                and date.fromisoformat(item["balance_as_of_date"]) <= point_as_of
            }
            total = 0
            for identifier in current_by_id:
                record = point.get(identifier)
                balance = None
                if record is not None and record["scheduled_balance_cents"] is not None:
                    balance = (
                        record["estimated_balance_cents"]
                        if record["observed_months"]
                        else record["scheduled_balance_cents"]
                    )
                    total += balance
                balance_by_id[identifier].append(balance)
            # No zero baseline is invented before the first tracked liability;
            # the chart starts when an actual loan is active.
            total_balances.append(total if point else None)

        observed_payments: list[int] = []
        scheduled_payments: list[int] = []
        for month in months:
            end = min(month_end(month), as_of)
            previous_end = date.fromisoformat(f"{month}-01") - timedelta(days=1)
            observed_total = 0
            scheduled_total = 0
            for record in current:
                anchor = date.fromisoformat(record["balance_as_of_date"])
                started = date.fromisoformat(record["start_date"])
                if anchor > end:
                    continue
                if month in record["payment_history"]:
                    observed_total += record["payment_history"][month]
                    continue
                due_at_anchor = scheduled_payments_due(started, anchor)
                due_before = min(
                    max(
                        0,
                        scheduled_payments_due(started, max(anchor, previous_end))
                        - due_at_anchor,
                    ),
                    record["term_months"],
                )
                due_by_end = min(
                    max(0, scheduled_payments_due(started, end) - due_at_anchor),
                    record["term_months"],
                )
                if due_by_end > due_before:
                    scheduled_total += record["contractual_monthly_cents"]
            observed_payments.append(observed_total)
            scheduled_payments.append(scheduled_total)

        loans = []
        total_pace = 0
        for record in current:
            balances = balance_by_id[record["id"]]
            active = [value for value in balances if value is not None]
            intervals = max(0, len(active) - 1)
            pace = 0 if not intervals else max(0, (active[0] - active[-1]) // intervals)
            total_pace += pace
            shown_balance = (
                record["estimated_balance_cents"]
                if record["observed_months"] else record["scheduled_balance_cents"]
            )
            loans.append({
                **record,
                "balance_cents": shown_balance,
                "repaid_cents": max(0, record["original_principal_cents"] - shown_balance),
                "repaid_percent": max(
                    0, min(100, (record["original_principal_cents"] - shown_balance)
                    * 100 / record["original_principal_cents"]),
                ),
                "paydown_pace_cents": pace,
                "payment_source": "observed" if record["observed_payment_cents"] else "scheduled",
                "balance_values": balances,
            })

        total_original = sum(item["original_principal_cents"] for item in loans)
        total_balance = sum(item["balance_cents"] for item in loans)
        payoff_values = [item["projected_payoff_months"] for item in loans]
        projected_payoff = (
            None if any(value is None for value in payoff_values)
            else max(payoff_values, default=0)
        )
        balance_series = [{"name": "Total remaining", "values": total_balances}]
        if len(loans) > 1:
            balance_series.extend(
                {"name": item["name"], "values": item["balance_values"]}
                for item in loans
            )
        return {
            "as_of": as_of.isoformat(),
            "months": months,
            "loans": loans,
            "total_original_cents": total_original,
            "total_balance_cents": total_balance,
            "total_repaid_cents": max(0, total_original - total_balance),
            "monthly_payment_cents": sum(
                item["projected_payment_cents"] for item in loans
                if item["balance_cents"] > 0
            ),
            "paydown_pace_cents": total_pace,
            "projected_payoff_months": projected_payoff,
            "balance_series": balance_series,
            "observed_payments": observed_payments,
            "scheduled_payments": scheduled_payments,
        }

    def delete_liability(self, identifier: int) -> None:
        with self._transaction() as connection:
            cursor = connection.execute("DELETE FROM liabilities WHERE id=?", (identifier,))
            self._require_record(cursor, "Loan no longer exists")

    def save_net_worth(self, item: NetWorthInput) -> int:
        assets_cents = parse_nonnegative_amount(item.assets, "Assets")
        liabilities_cents = parse_nonnegative_amount(item.liabilities, "Liabilities")
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO net_worth_snapshots(date, assets_cents, liabilities_cents, notes)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET assets_cents=excluded.assets_cents,
                       liabilities_cents=excluded.liabilities_cents, notes=excluded.notes,
                       updated_at=CURRENT_TIMESTAMP""",
                (item.date.isoformat(), assets_cents, liabilities_cents, item.notes.strip()),
            )
            row = connection.execute(
                "SELECT id FROM net_worth_snapshots WHERE date=?", (item.date.isoformat(),)
            ).fetchone()
        return int(row["id"])

    def dashboard(self, month: str, *, category_trend: dict | None = None) -> dict:
        if self._shared_connection is None:
            with self.read_session() as reader:
                return reader.dashboard(month, category_trend=category_trend)
        summary = self.summary(month, category_trend=category_trend)
        start, end = month_bounds(month)
        with self._connect() as connection:
            income = connection.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) total FROM income_entries WHERE date>=? AND date<?",
                (start, end),
            ).fetchone()["total"]
        estimate = self.income_estimate(month)
        income_is_estimated = income == 0 and estimate["amount_cents"] is not None
        display_income = estimate["amount_cents"] if income_is_estimated else income
        return {
            **summary,
            "income": income,
            "income_estimate": estimate,
            "display_income": display_income,
            "income_is_estimated": income_is_estimated,
            "outgoing": summary["total"],
            "net_cashflow": income - summary["total"],
            "display_net_cashflow": display_income - summary["total"],
            "net_worth": self.net_worth_at(month),
        }

    def list(self, month: str, search: str = "", category: str = "All", expense_type: str = "All",
             sort: str = "date") -> list[dict]:
        start, end = month_bounds(month)
        clauses = ["t.date >= ?", "t.date < ?"]
        params: list[object] = [start, end]
        if search.strip():
            clauses.append("lower(t.description) LIKE ?")
            params.append(f"%{search.strip().lower()}%")
        if category != "All":
            clauses.append("c.name = ?")
            params.append(category)
        if expense_type == "Regular":
            clauses.append("t.expense_type != ?")
            params.append(ANNUAL_EXPENSE_TYPE)
        elif expense_type == ANNUAL_EXPENSE_TYPE:
            clauses.append("t.expense_type = ?")
            params.append(ANNUAL_EXPENSE_TYPE)
        order_by = {
            "category": "c.sort_order, lower(t.subcategory), t.date DESC, t.id DESC",
            "amount": "t.amount_cents DESC, t.date DESC, t.id DESC",
        }.get(sort, "t.date DESC, t.id DESC")
        query = f"""SELECT t.id, t.date, t.amount_cents, t.transaction_kind,
                           t.description, c.name category,
                           t.subcategory, t.purpose, t.expense_type, t.need_want, t.notes,
                           t.spread_months, t.source_bank, t.source_vendor,
                           t.source_vendor_key, t.account_id,
                           a.name account_name, a.account_type, a.institution account_institution,
                           a.last_four account_last_four
                    FROM transactions t JOIN categories c ON c.id=t.category_id
                    LEFT JOIN accounts a ON a.id=t.account_id
                    WHERE {' AND '.join(clauses)} ORDER BY {order_by}"""
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def suggestions(self, prefix: str, limit: int = 6) -> list[dict]:
        suggestions, _ = self.description_assistance(
            self.description_catalog(), prefix, limit,
        )
        return suggestions

    def predicted_category(self, description: str) -> str | None:
        predicted = self.predicted_defaults(description)
        return predicted["category"] if predicted else None

    def predicted_defaults(self, description: str) -> dict | None:
        _, predicted = self.description_assistance(
            self.description_catalog(), description, 0,
        )
        return predicted

    def category_detail(self, month: str, category: str) -> dict:
        records = self.list(month, category=category, sort="amount")
        breakdown: dict[str, dict] = {}
        for record in records:
            label = record["subcategory"].strip() or record["description"].strip()
            normalized = normalize_description(label)
            detail = breakdown.setdefault(normalized, {"label": label, "total": 0})
            detail["total"] += record["amount_cents"]
        return {
            "total": sum(record["amount_cents"] for record in records),
            "breakdown": sorted(
                breakdown.values(),
                key=lambda item: (-item["total"], item["label"].casefold()),
            ),
            "expenses": records,
        }

    def monthly_comparison(self, end_month: str, category: str | None = None, count: int = 6) -> list[dict]:
        months = self.month_window(end_month, count)
        start, _ = month_bounds(months[0])
        _, end = month_bounds(months[-1])
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT substr(t.date, 1, 7) month, SUM(t.amount_cents) total
                   FROM transactions t JOIN categories c ON c.id=t.category_id
                   WHERE t.date>=? AND t.date<? AND (? IS NULL OR c.name = ?)
                   GROUP BY substr(t.date, 1, 7)""",
                (start, end, category, category),
            ).fetchall()
        totals = {row["month"]: row["total"] for row in rows}
        return [{"month": value, "total": totals.get(value, 0)} for value in months]

    def category_type_breakdown(self, month: str) -> dict:
        trend = self.category_trend(month, count=1)
        categories = [item["name"] for item in trend["series"]]
        return {
            "categories": categories,
            "series": [{
                "name": "Spending",
                "values": [item["values"][0] for item in trend["series"]],
            }],
        }

    def subcategory_type_breakdown(self, month: str, category: str) -> dict:
        if self._shared_connection is None:
            with self.read_session() as reader:
                return reader.subcategory_type_breakdown(month, category)
        breakdown = self.subcategory_comparison(month, category, count=1)["subcategories"]
        display_labels = {
            normalize_description(item["label"]): item["label"]
            for item in self.category_detail(month, category)["breakdown"]
        }
        return {
            "categories": [
                display_labels.get(
                    normalize_description(item["subcategory"]), item["subcategory"],
                )
                for item in breakdown
            ],
            "series": [{
                "name": "Spending",
                "values": [item["months"][0] for item in breakdown],
            }],
        }

    def category_trend(self, end_month: str, count: int = 12) -> dict:
        if self._shared_connection is None:
            with self.read_session() as reader:
                return reader.category_trend(end_month, count)
        months = self.month_window(end_month, count)
        source_start = shifted_month(months[0], -(ANNUAL_EXPENSE_MONTHS - 1))
        start, _ = month_bounds(source_start)
        _, end = month_bounds(months[-1])
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT substr(t.date, 1, 7) month, c.name category, t.amount_cents,
                          t.expense_type, t.spread_months
                   FROM transactions t JOIN categories c ON c.id=t.category_id
                   WHERE t.date>=? AND t.date<?""",
                (start, end),
            ).fetchall()
        values = self._spread_values(rows, months, "category")
        # Keep the same necessity-first category order everywhere. A stable stack
        # lets a band be followed from month to month without changing position.
        ordered = [
            category["name"] for category in self.category_settings()
            if category["name"] in values
        ]
        return {
            "months": months,
            "series": [
                {"name": category, "values": [values[category].get(month, 0) for month in months]}
                for category in ordered
            ],
        }

    @classmethod
    def _spread_values(cls, rows, months: list[str], label_key: str) -> dict[str, dict[str, int]]:
        values: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        visible_months = set(months)
        for row in rows:
            spread = (
                ANNUAL_EXPENSE_MONTHS
                if row["expense_type"] == ANNUAL_EXPENSE_TYPE else 1
            )
            allocations = allocate_cents(row["amount_cents"], spread)
            start_year, start_month = (int(part) for part in row["month"].split("-"))
            for offset in range(spread):
                absolute = start_year * 12 + start_month - 1 + offset
                allocated_month = f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"
                if allocated_month in visible_months:
                    values[row[label_key]][allocated_month] += allocations[offset]
        return values

    def net_worth_at(self, month: str, *, as_of: date | None = None) -> dict | None:
        """Return an actual or deterministic cash-flow estimate for a month end."""
        as_of = as_of or date.today()
        start, end = month_bounds(month)
        month_start = date.fromisoformat(start)
        if month_start > as_of:
            return None
        target = min(date.fromisoformat(end) - timedelta(days=1), as_of)
        with self._connect() as connection:
            snapshot = connection.execute(
                """SELECT date, assets_cents, liabilities_cents,
                          assets_cents-liabilities_cents net_worth
                   FROM net_worth_snapshots WHERE date<=? ORDER BY date DESC LIMIT 1""",
                (target.isoformat(),),
            ).fetchone()
            if not snapshot:
                return None
            actual = dict(snapshot)
            actual_date = date.fromisoformat(actual["date"])
            if actual_date.strftime("%Y-%m") == target.strftime("%Y-%m"):
                return {**actual, "estimated": False, "actual_date": actual["date"]}
            estimate = actual["net_worth"] + self._cashflow_between(
                connection, actual_date, target,
            )
        return {
            "date": target.isoformat(),
            "assets_cents": None,
            "liabilities_cents": None,
            "net_worth": estimate,
            "estimated": True,
            "actual_date": actual["date"],
            "actual_assets_cents": actual["assets_cents"],
            "actual_liabilities_cents": actual["liabilities_cents"],
        }

    def net_worth_trend(
        self, limit: int = 24, *, as_of: date | None = None,
    ) -> list[dict]:
        as_of = as_of or date.today()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT date, assets_cents, liabilities_cents,
                          assets_cents-liabilities_cents net_worth
                   FROM net_worth_snapshots WHERE date<=? ORDER BY date DESC LIMIT ?""",
                (as_of.isoformat(), limit),
            ).fetchall()
            history = [{**dict(row), "estimated": False} for row in rows][::-1]
            if not history:
                return []
            latest = history[-1]
            actual_date = date.fromisoformat(latest["date"])
            cashflow = self._cashflow_by_month(connection, actual_date, as_of)
            # Do not plot a second point in the actual snapshot's month. Carry
            # any later cash flow from that month into the first subsequent
            # month so the estimate still reconciles exactly to the ledger.
            cumulative = cashflow.get(actual_date.strftime("%Y-%m"), 0)
            for target in self._estimate_dates(actual_date, as_of):
                cumulative += cashflow.get(target.strftime("%Y-%m"), 0)
                history.append({
                    "date": target.isoformat(),
                    "assets_cents": None,
                    "liabilities_cents": None,
                    "net_worth": latest["net_worth"] + cumulative,
                    "estimated": True,
                    "actual_date": latest["date"],
                })
        return history[-limit:]

    @staticmethod
    def _estimate_dates(actual_date: date, as_of: date) -> list[date]:
        if actual_date >= as_of:
            return []
        targets: list[date] = []
        cursor = date.fromisoformat(
            shifted_month(actual_date.strftime("%Y-%m"), 1) + "-01"
        )
        current_month = as_of.replace(day=1)
        while cursor <= current_month:
            month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
            target = min(month_end, as_of)
            if target > actual_date:
                targets.append(target)
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)
        return targets

    @staticmethod
    def _cashflow_between(connection: Connection, start: date, end: date) -> int:
        return connection.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) total FROM (
                   SELECT amount_cents FROM income_entries WHERE date>? AND date<=?
                   UNION ALL
                   SELECT -amount_cents FROM transactions WHERE date>? AND date<=?
               )""",
            (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
        ).fetchone()["total"]

    @staticmethod
    def _cashflow_by_month(
        connection: Connection, start: date, end: date,
    ) -> dict[str, int]:
        return {
            row["month"]: row["total"]
            for row in connection.execute(
                """SELECT substr(date, 1, 7) month, SUM(amount_cents) total FROM (
                       SELECT date, amount_cents FROM income_entries WHERE date>? AND date<=?
                       UNION ALL
                       SELECT date, -amount_cents FROM transactions WHERE date>? AND date<=?
                   ) GROUP BY substr(date, 1, 7) ORDER BY month""",
                (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
            )
        }

    def category_movers(self, month: str, limit: int = 6) -> list[dict]:
        trend = self.category_trend(month, count=2)
        return self.category_movers_from_trend(trend, limit)

    @staticmethod
    def category_movers_from_trend(trend: dict, limit: int = 6) -> list[dict]:
        if len(trend["months"]) < 2:
            return []
        previous = {item["name"]: item["values"][-2] for item in trend["series"]}
        current = {item["name"]: item["values"][-1] for item in trend["series"]}
        movers = []
        for category in set(current) | set(previous):
            change = current.get(category, 0) - previous.get(category, 0)
            if change:
                movers.append({
                    "category": category,
                    "current": current.get(category, 0),
                    "previous": previous.get(category, 0),
                    "change": change,
                })
        movers.sort(key=lambda item: (-abs(item["change"]), item["category"].casefold()))
        return movers[:limit]

    def transaction_insights(
        self, end_month: str, category: str, count: int = 12,
    ) -> dict:
        """Load raw transaction rhythm once and derive explainable observations."""
        months = self.month_window(end_month, count)
        start, _ = month_bounds(months[0])
        _, end = month_bounds(months[-1])
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT t.id, t.date, t.amount_cents, t.transaction_kind,
                          t.description, t.source_vendor, t.source_vendor_key,
                          c.name category, c.sort_order
                   FROM transactions t JOIN categories c ON c.id=t.category_id
                   WHERE t.date>=? AND t.date<?
                   ORDER BY t.date, t.id""",
                (start, end),
            ).fetchall()
        return build_transaction_insights(rows, months, category)

    def subcategory_comparison(self, end_month: str, category: str, count: int = 6) -> dict:
        months = self.month_window(end_month, count)
        source_start = shifted_month(months[0], -(ANNUAL_EXPENSE_MONTHS - 1))
        start, _ = month_bounds(source_start)
        _, end = month_bounds(months[-1])
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT COALESCE(NULLIF(trim(t.subcategory), ''), trim(t.description)) detail,
                          substr(t.date, 1, 7) month, t.amount_cents,
                          t.expense_type, t.spread_months
                   FROM transactions t JOIN categories c ON c.id=t.category_id
                   WHERE c.name=? AND t.date>=? AND t.date<?
                   ORDER BY t.date DESC, t.amount_cents DESC, t.id DESC""",
                (category, start, end),
            ).fetchall()

        labels: dict[str, str] = {}
        normalized_rows: list[dict] = []
        for row in rows:
            key = normalize_description(row["detail"])
            labels.setdefault(key, row["detail"])
            normalized_rows.append({**dict(row), "detail_key": key})

        values = self._spread_values(normalized_rows, months, "detail_key")
        subcategories = [
            {
                "subcategory": labels[key],
                "months": [month_values.get(month, 0) for month in months],
                "total": sum(month_values.values()),
            }
            for key, month_values in values.items()
        ]
        subcategories.sort(
            key=lambda item: (-item["total"], item["subcategory"].casefold())
        )
        return {"months": months, "subcategories": subcategories}

    def summary(self, month: str, *, category_trend: dict | None = None) -> dict:
        if self._shared_connection is None and category_trend is None:
            with self.read_session() as reader:
                return reader.summary(month)
        start, end = month_bounds(month)
        with self._connect() as connection:
            by_type_rows = connection.execute(
                """SELECT expense_type, SUM(amount_cents) total,
                          COUNT(*) transaction_count
                   FROM transactions WHERE date >= ? AND date < ?
                   GROUP BY expense_type""",
                (start, end),
            ).fetchall()
            by_type = {row["expense_type"]: row["total"] for row in by_type_rows}
            by_category = [dict(row) for row in connection.execute(
                """SELECT c.name category, SUM(t.amount_cents) total FROM transactions t
                   JOIN categories c ON c.id=t.category_id WHERE t.date >= ? AND t.date < ?
                   GROUP BY c.name ORDER BY total DESC""", (start, end)
            )]
        total = sum(by_type.values())
        annual = by_type.get(ANNUAL_EXPENSE_TYPE, 0)
        regular = total - annual
        trend = category_trend or self.category_trend(month, count=2)
        normalized_trend = trend["series"]
        previous_normalized = sum(
            item["values"][-2] for item in normalized_trend if len(item["values"]) > 1
        )
        normalized = sum(item["values"][-1] for item in normalized_trend)
        return {"total": total, "by_type": by_type, "by_category": by_category,
                "regular": regular, "annual": annual, "normalized": normalized,
                "previous_normalized": previous_normalized,
                "transaction_count": sum(row["transaction_count"] for row in by_type_rows)}
