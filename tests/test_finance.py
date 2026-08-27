from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest

import finance_app.bank_import as bank_import_module
import finance_app.repository as repository_module
from finance_app.bank_import import SUPPORTED_BANKS, build_review_batch, decode_csv, parse_bank_csv
from finance_app.charts import net_worth_values
from finance_app import db as db_module
from finance_app.db import initialize
from finance_app.export import export_encrypted_backup, restore_encrypted_backup
from finance_app.formatting import money
from finance_app.import_policy import (
    MAX_CSV_BYTES, MAX_UPLOAD_REQUEST_BYTES, configure_memory_only_uploads,
    oversized_upload_request,
)
from finance_app.models import (
    AccountInput, BankImportMetadata, IncomeInput, NetWorthInput, TransactionInput,
)
from finance_app.repository import Repository
from finance_app.schema import CURRENT_SCHEMA_VERSION
from finance_app.services import (
    ANNUAL_EXPENSE_TYPE, EXPENSE_KIND, SETTLEMENT_KIND, allocate_cents, normalize_description,
    ngram_similarity, parse_amount, parse_transaction_amount, ranked_subcategory_options,
    shifted_month,
    parse_nonnegative_amount as parse_nonnegative_cents, subcategory_selection,
)
from finance_app.validation import (
    nonnegative_amount, positive_amount, required_date, required_text,
)
from finance_app.vault import prepare
from starlette.formparsers import MultiPartParser


def repository(tmp_path):
    path = tmp_path / "finance.db"
    initialize(path)
    return Repository(path)


def encrypted_repository(directory: Path, password: str) -> Repository:
    path = directory / "finance.db"
    prepare(path, password, password)
    initialize(path)
    return Repository(path)


def reviewed_import_metadata(batch, rows, account_id=None) -> BankImportMetadata:
    dates = [row.source.transaction_date for row in batch.rows]
    return BankImportMetadata(
        filename=batch.filename,
        bank=batch.bank,
        account_id=account_id,
        first_transaction_date=min(dates),
        last_transaction_date=max(dates),
        source_row_count=len(batch.rows),
        selected_row_count=len(rows),
    )


def single_import_metadata(item: TransactionInput, filename: str) -> BankImportMetadata:
    return BankImportMetadata(
        filename=filename,
        bank=item.source_bank,
        account_id=item.account_id,
        first_transaction_date=item.date,
        last_transaction_date=item.date,
        source_row_count=1,
        selected_row_count=1,
    )


def test_amount_and_description_normalization():
    assert money(-100000) == "-$1,000.00"
    assert money(10**20 + 1) == "$1,000,000,000,000,000,000.01"
    assert parse_amount("$1,234.56") == 123456
    assert parse_nonnegative_cents("0", "Liabilities") == 0
    assert parse_nonnegative_cents("1.005", "Assets") == 101
    assert normalize_description("  Costco   Gas ") == "costco gas"
    allocation = allocate_cents(10001, 12)
    assert allocation[:5] == (834, 834, 834, 834, 834)
    assert allocation[5:] == (833, 833, 833, 833, 833, 833, 833)
    assert sum(allocation) == 10001
    negative_allocation = allocate_cents(-10001, 12)
    assert negative_allocation[:5] == (-834, -834, -834, -834, -834)
    assert negative_allocation[5:] == (-833, -833, -833, -833, -833, -833, -833)
    assert sum(negative_allocation) == -10001
    assert parse_transaction_amount("1000", SETTLEMENT_KIND) == -100000
    assert parse_transaction_amount("-1000", SETTLEMENT_KIND) == -100000
    with pytest.raises(ValueError, match="Expense or Settlement"):
        parse_transaction_amount("10", "Refund")
    with pytest.raises(ValueError, match="valid amount"):
        parse_transaction_amount("not-money", SETTLEMENT_KIND)
    with pytest.raises(ValueError, match="greater than zero"):
        parse_transaction_amount("0", SETTLEMENT_KIND)
    with pytest.raises(ValueError, match="valid month"):
        shifted_month("2026-7", 1)
    with pytest.raises(ValueError, match="valid month"):
        shifted_month("2026-13", 1)


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_bank_amount_parser_rejects_non_finite_values(value):
    with pytest.raises(ValueError, match="Invalid transaction amount"):
        bank_import_module._amount(value)
    with pytest.raises(ValueError, match="Choose Settlement"):
        parse_transaction_amount("-10", EXPENSE_KIND)
    for non_finite in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="[Aa]mount"):
            parse_amount(non_finite)


def test_form_validation_contracts():
    assert positive_amount("12.50") == Decimal("12.50")
    assert nonnegative_amount("", "Liabilities", required=False) == Decimal("0")
    assert nonnegative_amount("0", "Assets") == Decimal("0")
    assert required_date("2026-07-03") == date(2026, 7, 3)
    assert required_text("  Costco  ", "Description") == "Costco"
    assert NetWorthInput(date(2026, 7, 3), Decimal("1000")).liabilities == Decimal("0")

    invalid_values = (
        (lambda: positive_amount(""), "Amount is required."),
        (lambda: nonnegative_amount("-1", "Liabilities"), "zero or more"),
        (lambda: required_date(""), "Date is required."),
        (lambda: required_text(" ", "Description"), "Description is required."),
    )
    for operation, expected in invalid_values:
        try:
            operation()
        except ValueError as error:
            assert expected in str(error)
        else:
            raise AssertionError("Expected validation to fail")


def test_category_order_runs_from_necessities_to_gifts(tmp_path):
    repo = repository(tmp_path)
    categories = repo.categories()
    assert categories[:3] == ["Groceries", "Housing", "Bills & Utilities"]
    assert categories[-1] == "Gifts"


def test_subcategory_selection_is_scoped_to_its_category():
    options = {
        "Groceries": ["Produce", "Warehouse"],
        "Shopping": ["Clothing"],
    }

    assert subcategory_selection(options, "Shopping", "Produce") == (
        ["Clothing"], "",
    )
    assert subcategory_selection(options, "Shopping", " clothing ") == (
        ["Clothing"], "Clothing",
    )
    assert subcategory_selection(
        options, "Shopping", "Seasonal", preserve_unknown=True,
    ) == (["Clothing", "Seasonal"], "Seasonal")

    library = [{
        "name": "Shopping",
        "subcategories": [
            {"name": "Seasonal", "is_active": 1, "transaction_count": 1, "sort_order": 0},
            {"name": "Clothing", "is_active": 1, "transaction_count": 4, "sort_order": 1},
            {"name": "Archived", "is_active": 0, "transaction_count": 8, "sort_order": 2},
        ],
    }]
    assert ranked_subcategory_options(library) == (
        {"Shopping": ["Clothing", "Seasonal"]}, {"Shopping": "Clothing"},
    )
    assert ngram_similarity("Northside Marketplace", "Northside Market") > 0.42
    assert ngram_similarity("Northside Marketplace", "City Transit") < 0.42


def test_transaction_subcategories_become_reusable_in_their_own_category(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 1), Decimal("12"), "Pop-up shop", "Shopping",
        subcategory="Seasonal",
    ))

    assert "Seasonal" in repo.subcategory_options("Shopping")
    assert "Seasonal" not in repo.subcategory_options("Groceries")


def test_month_shift_crosses_year_boundaries_deterministically():
    assert shifted_month("2026-01", -1) == "2025-12"
    assert shifted_month("2026-12", 1) == "2027-01"
    assert Repository.month_window("2026-01", 2) == ["2025-12", "2026-01"]
    with pytest.raises(ValueError, match="at least one"):
        Repository.month_window("2026-01", 0)
    with pytest.raises(ValueError, match="valid month"):
        Repository.month_window("not-a-month", 2)


def test_data_directory_can_be_configured_for_portable_builds(tmp_path, monkeypatch):
    portable_data = tmp_path / "portable-data"
    monkeypatch.setenv("EXPENSETICS_DATA_DIR", str(portable_data))
    assert db_module._default_data_dir() == portable_data.resolve()


def test_mutations_do_not_create_plaintext_csv_mirrors(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 10), Decimal("12.50"), "Test merchant", "Other",
    ))
    assert not list(tmp_path.glob("*.csv"))


def test_accounts_are_optional_and_archiving_preserves_transaction_history(tmp_path):
    repo = repository(tmp_path)
    unassigned_id = repo.add(TransactionInput(
        date(2026, 8, 1), Decimal("8.25"), "Cash purchase", "Other",
    ))
    account_id = repo.save_account(AccountInput(
        "Daily chequing", "Chequing", "Royal Bank of Canada", "4321",
    ))
    assigned_id = repo.add(TransactionInput(
        date(2026, 8, 2), Decimal("42.00"), "Groceries", "Groceries",
        account_id=account_id,
    ))

    rows = {row["id"]: row for row in repo.list("2026-08")}
    assert rows[unassigned_id]["account_id"] is None
    assert rows[assigned_id]["account_name"] == "Daily chequing"
    assert rows[assigned_id]["account_last_four"] == "4321"
    assert repo.last_manual_account_id() == account_id

    repo.set_account_active(account_id, False)
    assert repo.accounts() == []
    archived = repo.accounts(include_inactive=True)[0]
    assert archived["transaction_count"] == 1
    assert repo.list("2026-08")[0]["account_name"] == "Daily chequing"


def test_account_validation_and_bank_matching_are_deterministic(tmp_path):
    repo = repository(tmp_path)
    rbc_id = repo.save_account(AccountInput(
        "RBC Visa", "Credit card", "Royal Bank of Canada", "1111",
    ))
    repo.save_account(AccountInput("Cash wallet", "Cash"))
    accounts = repo.accounts()
    assert [row["id"] for row in repo.matching_accounts(accounts, "RBC")] == [rbc_id]
    assert repo.matching_accounts(accounts, "BMO") == []

    with pytest.raises(ValueError, match="exactly four"):
        repo.save_account(AccountInput("Bad card", "Credit card", "RBC", "12"))
    with pytest.raises(ValueError, match="unique account name"):
        repo.save_account(AccountInput("rbc visa", "Credit card", "RBC", "2222"))
    with pytest.raises(ValueError, match="valid account"):
        repo.add(TransactionInput(
            date(2026, 8, 3), Decimal("10"), "Unknown account", "Other",
            account_id=9999,
        ))


def test_recent_bank_imports_keep_only_encrypted_metadata_and_account_context(tmp_path):
    repo = repository(tmp_path)
    account_id = repo.save_account(AccountInput(
        "BMO Mastercard", "Credit card", "BMO", "4321",
    ))
    csv_text = """Item #,Card #,Transaction Date,Transaction Amount,Description
1,4321,20260702,12.34,COFFEE SHOP
2,4321,20260729,45.67,GROCERY STORE
"""
    batch = build_review_batch(
        csv_text, r"C:\Statements\july-transactions.csv", repo,
        bank="BMO", account_id=account_id,
    )
    selected = [row for row in batch.rows if row.include]

    assert repo.add_bank_import(
        [row.transaction(account_id) for row in selected],
        reviewed_import_metadata(batch, selected, account_id),
    ) == 2

    history = repo.recent_bank_imports()
    assert len(history) == 1
    assert {
        "filename": history[0]["filename"],
        "bank": history[0]["bank"],
        "account_name": history[0]["account_name"],
        "account_last_four": history[0]["account_last_four"],
        "first_transaction_date": history[0]["first_transaction_date"],
        "last_transaction_date": history[0]["last_transaction_date"],
        "source_row_count": history[0]["source_row_count"],
        "selected_row_count": history[0]["selected_row_count"],
        "imported_count": history[0]["imported_count"],
    } == {
        "filename": "july-transactions.csv",
        "bank": "BMO",
        "account_name": "BMO Mastercard",
        "account_last_four": "4321",
        "first_transaction_date": "2026-07-02",
        "last_transaction_date": "2026-07-29",
        "source_row_count": 2,
        "selected_row_count": 2,
        "imported_count": 2,
    }
    assert repo.recent_bank_imports(limit=0) == []
    assert not list(tmp_path.glob("*.csv"))

    next_import = TransactionInput(
        date(2026, 8, 3), Decimal("9"), "Later import", "Other",
        source_key="later-import", source_bank="BMO", source_vendor="Later",
        source_vendor_key="later", account_id=account_id,
    )
    assert repo.add_bank_import(
        [next_import], single_import_metadata(next_import, "august.csv"),
    ) == 1
    assert [row["filename"] for row in repo.recent_bank_imports()] == [
        "august.csv", "july-transactions.csv",
    ]
    assert repo.recent_bank_imports(limit=1)[0]["filename"] == "august.csv"

    invalid = TransactionInput(
        date(2026, 8, 1), Decimal("10"), "Invalid", "Missing category",
        source_key="invalid-import", source_bank="BMO", source_vendor="Invalid",
        source_vendor_key="invalid", account_id=account_id,
    )
    with pytest.raises(ValueError, match="Unknown category"):
        repo.add_bank_import(
            [invalid], single_import_metadata(invalid, "invalid.csv"),
        )
    assert len(repo.recent_bank_imports()) == 2

    with pytest.raises(ValueError, match="valid import filename"):
        repo.add_bank_import(
            [next_import], single_import_metadata(next_import, "invalid\nname.csv"),
        )
    assert len(repo.recent_bank_imports()) == 2

    repo.set_account_active(account_id, False)
    assert repo.recent_bank_imports()[0]["account_name"] == "BMO Mastercard"


def test_bank_import_duplicate_identity_is_scoped_to_the_selected_account(tmp_path):
    repo = repository(tmp_path)
    first_account = repo.save_account(AccountInput(
        "BMO personal", "Credit card", "BMO", "1111",
    ))
    second_account = repo.save_account(AccountInput(
        "BMO shared", "Credit card", "BMO", "2222",
    ))
    csv_text = """Item #,Card #,Transaction Date,Transaction Amount,Description
1,1111,20260802,12.34,COFFEE SHOP
"""

    first = build_review_batch(
        csv_text, "bmo.csv", repo, bank="BMO", account_id=first_account,
    )
    assert repo.add_bank_import(
        [first.rows[0].transaction(first_account)],
        reviewed_import_metadata(first, [first.rows[0]], first_account),
    ) == 1

    same_account = build_review_batch(
        csv_text, "bmo.csv", repo, bank="BMO", account_id=first_account,
    )
    other_account = build_review_batch(
        csv_text, "bmo.csv", repo, bank="BMO", account_id=second_account,
    )
    assert same_account.rows[0].locked
    assert same_account.rows[0].duplicate_reason == "Already imported"
    assert other_account.rows[0].include
    assert not other_account.rows[0].duplicate_reason
    assert first.rows[0].transaction(first_account).source_key != (
        first.rows[0].transaction(second_account).source_key
    )


def test_legacy_unscoped_import_identity_is_limited_to_its_original_account(tmp_path):
    repo = repository(tmp_path)
    first_account = repo.save_account(AccountInput(
        "BMO original", "Credit card", "BMO", "1111",
    ))
    second_account = repo.save_account(AccountInput(
        "BMO second", "Credit card", "BMO", "2222",
    ))
    csv_text = """Item #,Card #,Transaction Date,Transaction Amount,Description
1,1111,20260803,21.00,LEGACY STORE
"""
    source = parse_bank_csv(csv_text, "BMO")[0]
    repo.add(TransactionInput(
        source.transaction_date, source.amount, source.vendor, "Shopping",
        source_key=source.source_key, source_bank="BMO",
        source_vendor=source.vendor, source_vendor_key=source.vendor_key,
        account_id=first_account,
    ))

    original = build_review_batch(
        csv_text, "bmo.csv", repo, bank="BMO", account_id=first_account,
    )
    other = build_review_batch(
        csv_text, "bmo.csv", repo, bank="BMO", account_id=second_account,
    )
    assert original.rows[0].locked
    assert original.rows[0].duplicate_reason == "Already imported"
    assert other.rows[0].include


def test_accounts_and_transaction_links_round_trip_through_encrypted_backup(tmp_path):
    source_dir = tmp_path / "source"
    source = encrypted_repository(source_dir, "source vault password")
    account_id = source.save_account(AccountInput(
        "BMO Mastercard", "Credit card", "BMO", "9876",
    ))
    imported = TransactionInput(
        date(2026, 8, 4), Decimal("23.10"), "Portable account expense", "Shopping",
        source_key="portable-import", source_bank="BMO",
        source_vendor="Portable merchant", source_vendor_key="portable merchant",
        account_id=account_id,
    )
    source.add_bank_import(
        [imported], single_import_metadata(imported, "portable.csv"),
    )

    backup = export_encrypted_backup(
        tmp_path / "accounts-backup.expensetics", "export backup password", source.db_path,
    )
    target = encrypted_repository(tmp_path / "target", "target vault password")
    restore_encrypted_backup(backup, "export backup password", target.db_path)
    restored = target.list("2026-08")[0]
    assert restored["account_name"] == "BMO Mastercard"
    assert restored["account_last_four"] == "9876"
    restored_import = target.recent_bank_imports()[0]
    assert restored_import["filename"] == "portable.csv"
    assert restored_import["account_name"] == "BMO Mastercard"


def test_encrypted_backup_restores_a_complete_snapshot(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source = encrypted_repository(source_dir, "source vault password")
    source.add(TransactionInput(
        date(2026, 8, 3), Decimal("25.40"), "Portable expense", "Shopping",
        subcategory="Household",
    ))
    source.add_income(IncomeInput(date(2026, 8, 1), Decimal("1000"), "Salary"))
    source.save_net_worth(NetWorthInput(
        date(2026, 8, 10), Decimal("5000"), Decimal("1200"), "Laptop move",
    ))

    backup = export_encrypted_backup(
        tmp_path / "complete.expensetics", "backup export password", source.db_path,
    )
    target = encrypted_repository(target_dir, "target vault password")
    restore_encrypted_backup(backup, "backup export password", target.db_path)
    assert target.list("2026-08")[0]["description"] == "Portable expense"
    assert target.dashboard("2026-08")["income"] == 100000
    assert target.dashboard("2026-08")["net_worth"]["net_worth"] == 380000

    assert len(target.list("2026-08")) == 1


def test_settlement_round_trips_through_encrypted_backup(tmp_path):
    source_dir = tmp_path / "source"
    source = encrypted_repository(source_dir, "source vault password")
    source.add(TransactionInput(
        date(2026, 8, 8), Decimal("1000"), "Friend's travel share", "Travel",
        subcategory="Shared trip", transaction_kind=SETTLEMENT_KIND,
    ))

    backup = export_encrypted_backup(
        tmp_path / "settlement.expensetics", "backup export password", source.db_path,
    )
    target = encrypted_repository(tmp_path / "target", "target vault password")
    restore_encrypted_backup(backup, "backup export password", target.db_path)
    restored = target.list("2026-08")[0]
    assert restored["amount_cents"] == -100000
    assert restored["transaction_kind"] == SETTLEMENT_KIND


def test_encrypted_backup_preserves_legitimate_identical_transactions(tmp_path):
    source_dir = tmp_path / "source"
    source = encrypted_repository(source_dir, "source vault password")
    item = TransactionInput(
        date(2026, 8, 10), Decimal("12.50"), "Same purchase", "Shopping",
    )
    source.add(item)
    source.add(item)

    backup = export_encrypted_backup(
        tmp_path / "duplicates.expensetics", "backup export password", source.db_path,
    )
    target = encrypted_repository(tmp_path / "target", "target vault password")
    restore_encrypted_backup(backup, "backup export password", target.db_path)
    assert len(target.list("2026-08")) == 2


def test_packaged_data_directories_follow_platform_conventions(tmp_path):
    assert db_module._packaged_data_dir("darwin", tmp_path, {}) == (
        tmp_path / "Library" / "Application Support" / "Expensetics"
    )
    assert db_module._packaged_data_dir("win32", tmp_path, {"LOCALAPPDATA": "D:/Profiles/Local"}) == (
        db_module.Path("D:/Profiles/Local") / "Expensetics"
    )
    assert db_module._packaged_data_dir("linux", tmp_path, {}) == (
        tmp_path / ".local" / "share" / "Expensetics"
    )


def test_crud_prediction_and_summary_without_plaintext_exports(tmp_path):
    repo = repository(tmp_path)
    identifier = repo.add(TransactionInput(
        date(2026, 7, 2), Decimal("84.31"), "Costco", "Groceries", subcategory="Warehouse"
    ))
    repo.add(TransactionInput(date(2026, 7, 6), Decimal("10.00"), "costco", "Groceries"))
    repo.add(TransactionInput(date(2026, 7, 9), Decimal("20.00"), "Costco", "Shopping", expense_type="Discretionary"))
    repo.add(TransactionInput(date(2026, 7, 12), Decimal("215"), "Hotel", "Travel", expense_type="Travel"))

    assert repo.predicted_category("  COSTCO ") == "Groceries"
    assert repo.last_manual_date() == "2026-07-12"
    assert repo.suggestions("cos")[0]["description"] == "Costco"
    summary = repo.summary("2026-07")
    assert summary["total"] == 32931
    assert summary["transaction_count"] == 4
    assert summary["by_type"]["Travel"] == 21500
    assert repo.category_detail("2026-07", "Groceries")["total"] == 9431
    assert not (tmp_path / "transactions.csv").exists()

    repo.update(identifier, TransactionInput(date(2026, 7, 3), Decimal("90"), "Costco", "Groceries"))
    assert any(row["amount_cents"] == 9000 for row in repo.list("2026-07"))
    repo.delete(identifier)
    assert len(repo.list("2026-07")) == 3


def test_stale_mutations_fail_clearly_instead_of_appearing_successful(tmp_path):
    repo = repository(tmp_path)
    item = TransactionInput(date(2026, 7, 3), Decimal("10"), "Store", "Other")

    with pytest.raises(ValueError, match="no longer exists"):
        repo.update(999, item)
    with pytest.raises(ValueError, match="no longer exists"):
        repo.delete(999)
    with pytest.raises(ValueError, match="no longer exists"):
        repo.delete_liability(999)


def test_semantic_duplicates_use_the_documented_description_normalization(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 3), Decimal("10"), "Corner   Store", "Other",
    ))

    snapshot = repo.import_duplicate_snapshot(
        [], [], [date(2026, 7, 3)], [1000], None,
    )
    assert ("2026-07-03", 1000, "corner store") in snapshot["semantic_description"]


def test_month_comparison_cashflow_and_net_worth(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(date(2026, 7, 5), Decimal("25"), "Amazon", "Shopping", subcategory="Amazon"))
    repo.add(TransactionInput(date(2026, 8, 5), Decimal("40"), "Shoes", "Shopping", subcategory="Clothing"))
    repo.add_income(IncomeInput(date(2026, 8, 1), Decimal("1000"), "Salary"))
    repo.save_net_worth(NetWorthInput(date(2026, 8, 10), Decimal("5000"), Decimal("1200")))

    comparison = repo.monthly_comparison("2026-08", "Shopping", count=2)
    assert [item["total"] for item in comparison] == [2500, 4000]
    type_breakdown = repo.category_type_breakdown("2026-08")
    assert type_breakdown["categories"] == ["Shopping"]
    subcategory_types = repo.subcategory_type_breakdown("2026-08", "Shopping")
    assert subcategory_types["categories"] == ["Clothing"]
    category_trend = repo.category_trend("2026-08", count=2)
    assert category_trend["months"] == ["2026-07", "2026-08"]
    assert category_trend["series"][0]["values"] == [2500, 4000]
    subcategories = repo.subcategory_comparison("2026-08", "Shopping", count=2)
    assert subcategories["months"] == ["2026-07", "2026-08"]
    dashboard = repo.dashboard("2026-08")
    assert dashboard["income"] == 100000
    assert dashboard["outgoing"] == 4000
    assert dashboard["net_cashflow"] == 96000
    assert dashboard["net_worth"]["net_worth"] == 380000
    assert repo.net_worth_trend()[0]["net_worth"] == 380000
    assert not (tmp_path / "income.csv").exists()
    assert not (tmp_path / "net_worth.csv").exists()


def test_settlements_net_against_their_category_and_cashflow(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 2), Decimal("2000"), "Shared vacation", "Travel",
        subcategory="Friend trip",
    ))
    repo.add(TransactionInput(
        date(2026, 8, 12), Decimal("1000"), "Friend settlement", "Travel",
        subcategory="Friend trip", transaction_kind=SETTLEMENT_KIND,
    ))

    dashboard = repo.dashboard("2026-08")
    detail = repo.category_detail("2026-08", "Travel")
    assert dashboard["outgoing"] == 100000
    assert dashboard["net_cashflow"] == -100000
    assert detail["total"] == 100000
    assert detail["breakdown"] == [{"label": "Friend trip", "total": 100000}]
    assert sorted(row["amount_cents"] for row in repo.list("2026-08")) == [-100000, 200000]


def test_annual_settlement_is_allocated_over_twelve_months(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 1), Decimal("1200"), "Annual shared booking", "Travel",
        subcategory="Shared trip", expense_type=ANNUAL_EXPENSE_TYPE,
    ))
    repo.add(TransactionInput(
        date(2026, 8, 10), Decimal("600"), "Friend annual settlement", "Travel",
        subcategory="Shared trip", expense_type=ANNUAL_EXPENSE_TYPE,
        transaction_kind=SETTLEMENT_KIND,
    ))

    trend = repo.category_trend("2026-09", count=2)
    travel = next(item for item in trend["series"] if item["name"] == "Travel")
    assert travel["values"] == [5000, 5000]
    assert repo.dashboard("2026-08")["outgoing"] == 60000


def test_version_nine_database_migrates_without_changing_existing_records(tmp_path):
    path = tmp_path / "finance.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version(version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (9);
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO categories VALUES (1, 'Travel', 9, 1);
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, date TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            description TEXT NOT NULL, category_id INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT '', expense_type TEXT NOT NULL DEFAULT 'Living',
            need_want TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            source_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            subcategory TEXT NOT NULL DEFAULT '', spread_months INTEGER NOT NULL DEFAULT 1,
            source_bank TEXT NOT NULL DEFAULT '', source_vendor TEXT NOT NULL DEFAULT '',
            source_vendor_key TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO transactions (
            id, date, amount_cents, description, category_id, subcategory
        ) VALUES (42, '2026-07-10', 200000, 'Existing trip', 1, 'Shared trip');
        """
    )
    connection.commit()
    connection.close()

    initialize(path)
    migrated = Repository(path)
    existing = migrated.list("2026-07")[0]
    assert existing["id"] == 42
    assert existing["amount_cents"] == 200000
    assert existing["transaction_kind"] == EXPENSE_KIND
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_import_history'"
        ).fetchone() is not None
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(transactions)")
        }
    assert {
        "idx_transactions_date", "idx_transactions_description",
        "idx_transactions_category_date", "idx_transactions_duplicate",
    } <= indexes

    migrated.add(TransactionInput(
        date(2026, 7, 15), Decimal("1000"), "Settlement", "Travel",
        transaction_kind=SETTLEMENT_KIND,
    ))
    assert migrated.dashboard("2026-07")["outgoing"] == 100000


def test_schema_initialization_rolls_back_every_change_when_migration_fails(
    tmp_path, monkeypatch,
):
    path = tmp_path / "finance.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (9);
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO categories VALUES (1, 'Other', 0, 1);
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                description TEXT NOT NULL, category_id INTEGER NOT NULL,
                purpose TEXT NOT NULL DEFAULT '', expense_type TEXT NOT NULL DEFAULT 'Living',
                need_want TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                source_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO transactions (
                id, date, amount_cents, description, category_id
            ) VALUES (1, '2026-08-01', 1000, 'Atomic migration', 1);
            """
        )

    def fail_after_schema_change(connection) -> None:
        connection.execute("CREATE TABLE migration_probe(value INTEGER)")
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(db_module, "_migrate_transactions_v10", fail_after_schema_change)
    with pytest.raises(RuntimeError, match="simulated"):
        initialize(path)

    with sqlite3.connect(path) as connection:
        table_names = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
    assert "migration_probe" not in table_names
    assert "accounts" not in table_names
    assert "subcategory" not in columns
    assert version == 9


def test_category_detail_uses_subcategory_then_vendor_without_double_counting(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 2), Decimal("40"), "Store A", "Shopping", subcategory="Clothing",
    ))
    repo.add(TransactionInput(
        date(2026, 8, 3), Decimal("10"), "Store B", "Shopping", subcategory="clothing",
    ))
    repo.add(TransactionInput(
        date(2026, 8, 4), Decimal("25"), "Store A", "Shopping",
    ))

    detail = repo.category_detail("2026-08", "Shopping")
    assert detail["breakdown"] == [
        {"label": "Clothing", "total": 5000},
        {"label": "Store A", "total": 2500},
    ]
    assert sum(item["total"] for item in detail["breakdown"]) == detail["total"] == 7500

    chart = repo.subcategory_type_breakdown("2026-08", "Shopping")
    assert chart == {
        "categories": ["Clothing", "Store A"],
        "series": [{"name": "Spending", "values": [5000, 2500]}],
    }

    comparison = repo.subcategory_comparison("2026-08", "Shopping", count=1)
    assert comparison["subcategories"] == [
        {"subcategory": "clothing", "months": [5000], "total": 5000},
        {"subcategory": "Store A", "months": [2500], "total": 2500},
    ]


def test_committed_transaction_requires_no_export_side_effect(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 2), Decimal("12.50"), "Costco", "Groceries",
    ))

    assert repo.list("2026-07")[0]["amount_cents"] == 1250
    assert not list(tmp_path.glob("*.csv"))


def test_annual_expense_is_allocated_over_exactly_twelve_months(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 15), Decimal("120.00"), "Annual membership", "Shopping",
        subcategory="Membership", expense_type="One-off",
    ))

    assert repo.summary("2026-07")["total"] == 12000
    trend = repo.category_trend("2026-09", count=3)
    assert trend["series"] == [{"name": "Shopping", "values": [1000, 1000, 1000]}]
    assert repo.summary("2026-08")["normalized"] == 1000
    assert repo.summary("2026-08")["previous_normalized"] == 1000
    assert repo.list("2026-07")[0]["spread_months"] == 12


def test_category_movers_compare_the_two_latest_visible_months() -> None:
    trend = {
        "months": ["2026-06", "2026-07", "2026-08"],
        "series": [
            {"name": "Groceries", "values": [1_000, 4_000, 5_000]},
            {"name": "Dining", "values": [9_000, 2_000, 1_500]},
        ],
    }

    assert Repository.category_movers_from_trend(trend) == [
        {"category": "Groceries", "previous": 4_000, "current": 5_000, "change": 1_000},
        {"category": "Dining", "previous": 2_000, "current": 1_500, "change": -500},
    ]


def test_overview_breakdowns_use_monthly_equivalents_with_or_without_budgets(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 2), Decimal("1200"), "Annual service", "Shopping",
        subcategory="Car service", expense_type=ANNUAL_EXPENSE_TYPE,
    ))

    category = repo.category_type_breakdown("2026-08")
    detail = repo.subcategory_type_breakdown("2026-08", "Shopping")
    assert category["series"][0]["values"] == [10_000]
    assert detail["series"][0]["values"] == [10_000]

    repo.save_budgets("2026-08", {"Shopping": "150"})
    budget = next(
        row for row in repo.budgets("2026-08") if row["category"] == "Shopping"
    )
    assert budget["actual_cents"] == 10_000
    overview_budget = next(
        row for row in repo.budgets(
            "2026-08", category_trend=repo.category_trend("2026-08", count=12),
        )
        if row["category"] == "Shopping"
    )
    assert overview_budget["actual_cents"] == 10_000
    assert overview_budget["remaining_cents"] == 5_000
    assert repo.category_type_breakdown("2026-08") == category


def test_bmo_adapter_parses_preamble_and_excludes_credits(tmp_path):
    repo = repository(tmp_path)
    csv_text = """Following data is valid as of 20260810165109:,,,,,
,,,,,
Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,5524890045782513,20260520,20260521,-25.0,Cashback/Remises CR
2,5524890045782513,20260523,20260525,7.5,COBS Bread TORONTO ON
3,5524890045782513,20260607,20260609,67.31,SHELL C02146 TORONTO ON
"""
    parsed = parse_bank_csv(csv_text, "BMO")
    assert [row.bank for row in parsed] == ["BMO", "BMO", "BMO"]
    assert parsed[0].eligible is False
    assert parsed[1].transaction_date == date(2026, 5, 23)
    assert parsed[2].vendor_key == "shell"

    batch = build_review_batch(csv_text, "bmo.csv", repo, bank="BMO")
    assert batch.rows[0].locked is True
    assert batch.rows[1].category == "Groceries"
    assert batch.rows[2].description == "Gas"
    assert batch.rows[2].subcategory == "Gas"

    batch.rows[2].annual_expense = True
    assert repo.add_bank_import(
        [batch.rows[2].transaction()],
        reviewed_import_metadata(batch, [batch.rows[2]]),
    ) == 1
    imported = repo.list("2026-06")[0]
    assert imported["expense_type"] == "One-off"
    assert imported["spread_months"] == 12


def test_bank_history_replaces_category_and_subcategory_as_one_pair(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 5, 1), Decimal("5"), "Gas", "Shopping",
    ))
    csv_text = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260810,20260811,67.31,SHELL C02146 TORONTO ON
"""

    row = build_review_batch(csv_text, "bmo.csv", repo, bank="BMO").rows[0]

    assert row.suggestion_source == "Matched expense history"
    assert (row.category, row.subcategory) == ("Shopping", "")


def test_bank_fallback_clears_subcategory_from_an_inactive_category(tmp_path):
    repo = repository(tmp_path)
    transportation = next(
        category for category in repo.category_library()
        if category["name"] == "Transportation"
    )
    repo.set_category_active(transportation["id"], False)
    csv_text = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260810,20260811,67.31,SHELL C02146 TORONTO ON
"""

    row = build_review_batch(csv_text, "bmo.csv", repo, bank="BMO").rows[0]

    assert (row.category, row.subcategory) == ("Other", "")
    assert row.suggestion_source == "Needs review · Transportation is archived"
    assert row.needs_category_review is True
    assert row.include is False


def test_bank_review_uses_ngram_history_then_category_favorite(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 5, 1), Decimal("15"), "Northside Market", "Groceries",
        subcategory="Organic",
    ))
    similar_csv = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260810,20260811,22.10,NORTHSIDE MARKETPLACE
"""

    similar = build_review_batch(
        similar_csv, "similar.csv", repo, bank="BMO",
    ).rows[0]

    assert (similar.category, similar.subcategory) == ("Groceries", "Organic")
    assert similar.suggestion_source.startswith("Matched similar expense history")

    repo.add(TransactionInput(
        date(2026, 5, 2), Decimal("10"), "Prior A", "Other",
        subcategory="Household",
    ))
    repo.add(TransactionInput(
        date(2026, 5, 3), Decimal("11"), "Prior B", "Other",
        subcategory="Household",
    ))
    repo.add(TransactionInput(
        date(2026, 5, 4), Decimal("12"), "Prior C", "Other",
        subcategory="Miscellaneous",
    ))
    unrelated_csv = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260812,20260813,30.00,ZZQ NEW VENDOR
"""

    fallback = build_review_batch(
        unrelated_csv, "unrelated.csv", repo, bank="BMO",
    ).rows[0]

    assert (fallback.category, fallback.subcategory) == ("Other", "Household")
    assert fallback.suggestion_source == "Most used subcategory in Other"


def test_learned_vendor_choice_can_follow_the_vendor_across_banks(tmp_path):
    repo = repository(tmp_path)
    imported = TransactionInput(
        date(2026, 7, 1), Decimal("45"), "Mobile plan", "Shopping",
        subcategory="Cellular", source_key="rogers-fido-1", source_bank="Rogers",
        source_vendor="FIDO Mobile", source_vendor_key="fido",
    )
    repo.add_bank_import(
        [imported], single_import_metadata(imported, "rogers-history.csv"),
    )
    bmo_csv = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260810,20260811,46.00,FIDO MOBILE
"""

    row = build_review_batch(bmo_csv, "bmo.csv", repo, bank="BMO").rows[0]

    assert (row.description, row.category, row.subcategory) == (
        "Mobile plan", "Shopping", "Cellular",
    )
    assert row.suggestion_source.startswith("Learned from")


def test_bank_review_uses_one_scoped_database_connection(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    original_connect = repository_module.connect
    connection_count = 0

    def counted_connect(*args, **kwargs):
        nonlocal connection_count
        connection_count += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(repository_module, "connect", counted_connect)
    csv_text = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260810,20260811,12.00,SHOP
2,0000000000000000,20260811,20260812,8.00,CAFE
"""

    batch = build_review_batch(csv_text, "bmo.csv", repo, bank="BMO")

    assert len(batch.rows) == 2
    assert connection_count == 1


def test_bulk_import_lookups_chunk_without_opening_more_connections(
    tmp_path, monkeypatch,
):
    repo = repository(tmp_path)
    original_connect = repository_module.connect
    connection_count = 0

    def counted_connect(*args, **kwargs):
        nonlocal connection_count
        connection_count += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(repository_module, "connect", counted_connect)
    keys = [f"source-{index}" for index in range(901)]
    with repo.read_session() as reader:
        assert reader.vendor_preferences("BMO", keys) == {}
        snapshot = reader.import_duplicate_snapshot(
            keys, [f"legacy-{index}" for index in range(901)],
            [date(2026, 8, 1)] * 901, list(range(1, 902)), None,
        )

    assert snapshot["sources"] == set()
    assert connection_count == 1


def test_bank_csv_resource_limits_are_enforced_before_review(monkeypatch):
    monkeypatch.setattr(bank_import_module, "MAX_CSV_BYTES", 8)
    with pytest.raises(ValueError, match="safety limit"):
        decode_csv(b"123456789")

    monkeypatch.setattr(bank_import_module, "MAX_CSV_ROWS", 2)
    with pytest.raises(ValueError, match="row review limit"):
        bank_import_module._read_csv_rows("a,b\n1,2\n3,4\n")


def test_accepted_csv_uploads_cannot_spool_to_a_readable_temp_file(monkeypatch):
    monkeypatch.setattr(MultiPartParser, "spool_max_size", 1024)
    configure_memory_only_uploads()
    assert MultiPartParser.spool_max_size == MAX_UPLOAD_REQUEST_BYTES
    assert not oversized_upload_request(
        "POST", "/_nicegui/client/1/upload/2", str(MAX_UPLOAD_REQUEST_BYTES),
    )
    assert oversized_upload_request(
        "POST", "/_nicegui/client/1/upload/2", str(MAX_UPLOAD_REQUEST_BYTES + 1),
    )
    assert oversized_upload_request("POST", "/_nicegui/client/1/upload/2", None)
    assert not oversized_upload_request("GET", "/", None)


BANK_FIXTURE_CASES = (
    ("American Express (US)", "american_express.csv", "COFFEE SHOP"),
    ("Apple Card", "apple_card.csv", "COFFEE SHOP"),
    ("BMO", "bmo.csv", "COFFEE SHOP"),
    ("Bank of America", "bank_of_america.csv", "COFFEE SHOP"),
    ("Capital One", "capital_one.csv", "COFFEE SHOP"),
    ("Chase", "chase.csv", "COFFEE SHOP"),
    ("CIBC", "cibc.csv", "COFFEE SHOP"),
    ("Citi", "citi.csv", "COFFEE SHOP"),
    ("Desjardins", "desjardins.csv", "COFFEE SHOP"),
    ("Discover", "discover.csv", "COFFEE SHOP"),
    ("RBC", "rbc.csv", "COFFEE SHOP · TORONTO"),
    ("Rogers", "rogers.csv", "COFFEE SHOP"),
    ("Scotiabank", "scotiabank.csv", "COFFEE SHOP · TORONTO"),
    ("TD", "td.csv", "COFFEE SHOP"),
    ("U.S. Bank", "us_bank.csv", "COFFEE SHOP · TORONTO"),
    ("Wells Fargo", "wells_fargo.csv", "COFFEE SHOP"),
    ("Monzo", "monzo.csv", "COFFEE SHOP"),
    ("N26", "n26.csv", "COFFEE SHOP"),
    ("Rabobank", "rabobank_transactions.csv", "COFFEE SHOP"),
    ("Revolut Business", "revolut_business.csv", "COFFEE SHOP"),
    ("Starling", "starling.csv", "COFFEE SHOP"),
    ("Wise", "wise.csv", "COFFEE SHOP"),
    ("bunq", "bunq.csv", "COFFEE SHOP"),
    ("MUFG BizSTATION", "mufg_bizstation.csv", "COFFEE SHOP"),
    ("Mizuho Business WEB", "mizuho_business_web.csv", "COFFEE SHOP"),
    ("SMBC Direct", "smbc_direct.csv", "COFFEE SHOP"),
)


@pytest.mark.parametrize(
    ("bank", "fixture_name", "expected_vendor"), BANK_FIXTURE_CASES,
)
def test_additional_bank_adapters_parse_purchases_and_exclude_credits(
    bank: str, fixture_name: str, expected_vendor: str,
) -> None:
    csv_text = (
        Path(__file__).parent / "fixtures" / "bank_csv" / fixture_name
    ).read_text(encoding="utf-8")
    rows = parse_bank_csv(csv_text, bank)
    assert rows[0].vendor == expected_vendor
    expected_amount = Decimal("1234") if bank in {
        "MUFG BizSTATION", "Mizuho Business WEB", "SMBC Direct",
    } else Decimal("12.34")
    assert rows[0].amount == expected_amount
    assert rows[0].eligible is True
    assert rows[1].eligible is False


def test_supported_bank_registry_is_explicit_and_stable() -> None:
    assert SUPPORTED_BANKS == (
        "American Express (US)", "Apple Card", "BMO", "Bank of America",
        "Capital One", "Chase", "CIBC", "Citi", "Desjardins", "Discover",
        "RBC", "Rogers", "Scotiabank", "TD", "U.S. Bank", "Wells Fargo",
        "Monzo", "N26", "Rabobank", "Revolut Business", "Starling", "Wise", "bunq",
        "MUFG BizSTATION", "Mizuho Business WEB", "SMBC Direct",
    )
    assert tuple(case[0] for case in BANK_FIXTURE_CASES) == SUPPORTED_BANKS


def test_bunq_adapter_accepts_semicolon_csv_and_decimal_comma() -> None:
    csv_text = (
        Path(__file__).parent / "fixtures" / "bank_csv" / "bunq.csv"
    ).read_text(encoding="utf-8")
    row = parse_bank_csv(csv_text, "bunq")[0]
    assert row.transaction_date == date(2026, 8, 2)
    assert row.amount == Decimal("12.34")


@pytest.mark.parametrize("fixture_name", (
    "mufg_bizstation.csv", "mizuho_business_web.csv", "smbc_direct.csv",
))
def test_japanese_bank_exports_decode_from_documented_shift_jis(fixture_name: str) -> None:
    text = (Path(__file__).parent / "fixtures" / "bank_csv" / fixture_name).read_text(
        encoding="utf-8",
    )
    decoded = decode_csv(text.encode("cp932"))
    assert "COFFEE SHOP" in decoded
    assert any(character in decoded for character in ("勘定日", "日付", "全明細"))


def test_mizuho_and_smbc_require_only_consumed_named_columns() -> None:
    mizuho = (
        '"勘定日","出金（円）","入金（円）","摘要","将来の追加列"\n'
        '"2026年8月2日","1,234","-","COFFEE SHOP","ignored"\n'
    )
    smbc = (
        '"日付","お引出し","お預入れ","お取引内容","追加列"\n'
        '"2026/08/02","1,234","－","COFFEE SHOP","ignored"\n'
    )
    assert parse_bank_csv(mizuho, "Mizuho Business WEB")[0].amount == Decimal("1234")
    assert parse_bank_csv(smbc, "SMBC Direct")[0].amount == Decimal("1234")


MINIMAL_HEADER_CASES = (
    ("Rogers", "Date,Merchant Name,Amount\n2026-08-02,COFFEE SHOP,12.34\n"),
    ("Capital One", "Transaction Date,Description,Debit\n2026-08-02,COFFEE SHOP,12.34\n"),
    ("Chase", "Posting Date,Description,Amount\n08/02/2026,COFFEE SHOP,-12.34\n"),
    ("Citi", "Date,Description,Debit\n08/02/2026,COFFEE SHOP,12.34\n"),
    ("Apple Card", "Transaction Date,Merchant,Type,Amount (USD)\n08/02/2026,COFFEE SHOP,Purchase,12.34\n"),
    ("Discover", "Trans. Date,Description,Amount\n08/02/2026,COFFEE SHOP,12.34\n"),
    ("U.S. Bank", "Date,Name,Amount\n08/02/2026,COFFEE SHOP,-12.34\n"),
    ("TD", "Date,Description,Withdrawals\n08/02/2026,COFFEE SHOP,12.34\n"),
    ("CIBC", "Transaction Date,Description,Withdrawals\n2026-08-02,COFFEE SHOP,12.34\n"),
    ("Scotiabank", "Date,Description,Amount\n2026-08-02,COFFEE SHOP,-12.34\n"),
    ("RBC", "Transaction Date,Description 1,CAD$\n2026-08-02,COFFEE SHOP,-12.34\n"),
    ("Monzo", "Date,Name,Amount\n2026-08-02,COFFEE SHOP,-12.34\n"),
    ("Revolut Business", "Completed Date,Description,Amount,State\n2026-08-02,COFFEE SHOP,-12.34,COMPLETED\n"),
    ("Starling", "Date,Counter Party,Amount (GBP)\n2026-08-02,COFFEE SHOP,-12.34\n"),
    ("Wise", "Date,Amount,Description\n2026-08-02,-12.34,COFFEE SHOP\n"),
    ("Rabobank", "Datum,Bedrag,Naam tegenpartij\n2026-08-02,-12.34,COFFEE SHOP\n"),
    ("Rabobank", "Creditcard Nummer,Datum,Bedrag,Omschrijving\n1234,2026-08-02,-12.34,COFFEE SHOP\n"),
    ("bunq", "Date,Amount,Counterparty\n2026-08-02,-12.34,COFFEE SHOP\n"),
    ("Mizuho Business WEB", "勘定日,出金（円）,摘要\n2026年8月2日,1234,COFFEE SHOP\n"),
    ("SMBC Direct", "日付,お引出し,お取引内容\n2026/08/02,1234,COFFEE SHOP\n"),
)


@pytest.mark.parametrize(("bank", "csv_text"), MINIMAL_HEADER_CASES)
def test_headered_bank_adapters_require_only_consumed_columns(
    bank: str, csv_text: str,
) -> None:
    row = parse_bank_csv(csv_text, bank)[0]
    assert row.vendor == "COFFEE SHOP"
    assert row.amount > 0


@pytest.mark.parametrize("bank", ("MUFG BizSTATION", "Mizuho Business WEB", "SMBC Direct"))
def test_asian_bank_adapters_reject_a_different_selected_format(bank: str) -> None:
    with pytest.raises(ValueError, match="CSV format"):
        parse_bank_csv("Date,Description,Amount\n2026-08-02,Coffee,-12.34\n", bank)


def test_revolut_business_adapter_excludes_incomplete_transactions() -> None:
    csv_text = (
        "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
        "CARD,Current,2026-08-02 12:30:00,2026-08-02 12:31:00,COFFEE SHOP,-12.34,0,EUR,PENDING,987.66\n"
    )
    row = parse_bank_csv(csv_text, "Revolut Business")[0]
    assert row.eligible is False
    assert row.exclusion_reason == "Transaction state is pending"


def test_revolut_business_expenses_adapter_uses_official_columns() -> None:
    csv_text = (
        Path(__file__).parent / "fixtures" / "bank_csv" / "revolut_business_expenses.csv"
    ).read_text(encoding="utf-8")
    rows = parse_bank_csv(csv_text, "Revolut Business")
    assert rows[0].reference == "rev-exp-001"
    assert rows[0].merchant_category == "Meals"
    assert rows[0].amount == Decimal("12.34")
    assert rows[0].eligible is True
    assert rows[1].eligible is False


def test_rabobank_credit_card_adapter_uses_official_sign_contract() -> None:
    csv_text = (
        Path(__file__).parent / "fixtures" / "bank_csv" / "rabobank_credit_card.csv"
    ).read_text(encoding="utf-8")
    rows = parse_bank_csv(csv_text, "Rabobank")
    assert rows[0].reference.endswith(":1234:card-001")
    assert rows[0].amount == Decimal("12.34")
    assert rows[0].eligible is True
    assert rows[1].eligible is False


def test_rabobank_english_account_contract_is_explicitly_supported() -> None:
    csv_text = (
        Path(__file__).parent / "fixtures" / "bank_csv" / "rabobank_transactions_en.csv"
    ).read_text(encoding="utf-8")
    row = parse_bank_csv(csv_text, "Rabobank")[0]
    assert row.vendor == "COFFEE SHOP"
    assert row.amount == Decimal("12.34")
    assert row.reference.endswith(":EUR:000000000000001001")


def test_rabobank_rejects_a_partial_lookalike_header() -> None:
    with pytest.raises(ValueError, match="Rabobank CSV format"):
        parse_bank_csv("Datum,Bedrag,Omschrijving\n2026-08-02,-12.34,Coffee\n", "Rabobank")


@pytest.mark.parametrize(("bank", "fixture_name", "valid_date", "wrong_date"), (
    ("American Express (US)", "american_express.csv", "08/02/2026", "2026-08-02"),
    ("Apple Card", "apple_card.csv", "08/02/2026", "2026-08-02"),
    ("Discover", "discover.csv", "08/02/2026", "2026-08-02"),
    ("U.S. Bank", "us_bank.csv", "08/02/2026", "2026-08-02"),
    ("Desjardins", "desjardins.csv", "2026/08/02", "08/02/2026"),
))
def test_bank_specific_date_contracts_reject_other_formats(
    bank: str, fixture_name: str, valid_date: str, wrong_date: str,
) -> None:
    csv_text = (
        Path(__file__).parent / "fixtures" / "bank_csv" / fixture_name
    ).read_text(encoding="utf-8").replace(valid_date, wrong_date, 1)
    with pytest.raises(ValueError, match="date|format"):
        parse_bank_csv(csv_text, bank)


def test_desjardins_visa_uses_advance_and_reimbursement_columns() -> None:
    csv_text = (
        '"Visa 4540 0000 0000 0000","","","2026/08/02",1,'
        '"COFFEE SHOP","","","","",12.34,"",987.66\n'
        '"Visa 4540 0000 0000 0000","","","2026/08/03",2,'
        '"REFUND","","","","","",20.00,1007.66\n'
    )
    rows = parse_bank_csv(csv_text, "Desjardins")
    assert rows[0].amount == Decimal("12.34")
    assert rows[0].eligible is True
    assert rows[1].eligible is False


def test_net_worth_estimates_use_only_recorded_cashflow(tmp_path):
    repo = repository(tmp_path)
    repo.save_net_worth(NetWorthInput(date(2026, 1, 31), Decimal("1000")))
    repo.add_income(IncomeInput(date(2026, 2, 1), Decimal("500"), "Income"))
    repo.add(TransactionInput(
        date(2026, 2, 10), Decimal("200"), "Expense", "Other",
    ))
    repo.add_income(IncomeInput(date(2026, 3, 1), Decimal("50"), "Income"))
    repo.add(TransactionInput(
        date(2026, 3, 10), Decimal("100"), "Expense", "Other",
    ))

    history = repo.net_worth_trend(as_of=date(2026, 3, 15))
    assert [(item["date"], item["net_worth"], item["estimated"]) for item in history] == [
        ("2026-01-31", 100000, False),
        ("2026-02-28", 130000, True),
        ("2026-03-15", 125000, True),
    ]
    estimate = repo.net_worth_at("2026-03", as_of=date(2026, 3, 15))
    assert estimate["net_worth"] == 125000
    assert estimate["actual_date"] == "2026-01-31"
    assert estimate["assets_cents"] is None
    actual_values, estimated_values = net_worth_values(history)
    assert actual_values == [1000.0, None, None]
    assert estimated_values == [None, 1300.0, 1250.0]

    repo.save_net_worth(NetWorthInput(date(2026, 3, 15), Decimal("1500")))
    assert repo.net_worth_at("2026-03", as_of=date(2026, 3, 15))["estimated"] is False


def test_net_worth_estimates_begin_after_the_actual_snapshot_month(tmp_path):
    repo = repository(tmp_path)
    repo.save_net_worth(NetWorthInput(date(2026, 1, 15), Decimal("1000")))
    repo.add_income(IncomeInput(date(2026, 1, 20), Decimal("100"), "Income"))
    repo.add_income(IncomeInput(date(2026, 2, 1), Decimal("500"), "Income"))
    repo.add(TransactionInput(
        date(2026, 2, 10), Decimal("200"), "Expense", "Other",
    ))

    january = repo.net_worth_at("2026-01", as_of=date(2026, 1, 31))
    assert january["date"] == "2026-01-15"
    assert january["estimated"] is False

    history = repo.net_worth_trend(as_of=date(2026, 2, 15))
    assert [(item["date"], item["net_worth"], item["estimated"]) for item in history] == [
        ("2026-01-15", 100_000, False),
        ("2026-02-15", 140_000, True),
    ]


def test_rogers_review_deduplicates_and_learns_recent_vendor_choices(tmp_path):
    repo = repository(tmp_path)
    header = (
        "Date,Posted Date,Reference Number,Activity Type,Activity Status,Card Number,"
        "Merchant Category Description,Merchant Name,Merchant City,Merchant State or Province,"
        "Merchant Country Code,Merchant Postal Code,Amount,Rewards,Name on Card\n"
    )
    category = (
        "Telecommunication Services Including Local and Long Distance Calls Credit Card Calls"
    )
    first_csv = header + (
        f'2026-06-04,2026-06-05,ref-1,TRANS,APPROVED,************8927,'
        f'"{category}",FIDO Mobile ******4890,TORONTO,ON,CAN,M4Y2Y5,$45.20,,User\n'
        f'2026-06-04,2026-06-05,ref-2,TRANS,APPROVED,************8927,'
        f'"{category}",FIDO Mobile ******4890,TORONTO,ON,CAN,M4Y2Y5,$45.20,,User\n'
    )
    batch = build_review_batch(first_csv, "rogers.csv", repo, bank="Rogers")
    assert batch.bank == "Rogers"
    assert batch.rows[0].description == "Phone bill"
    assert batch.rows[0].category == "Bills & Utilities"
    assert batch.rows[0].include is True
    assert batch.rows[1].include is False
    assert "this file" in batch.rows[1].duplicate_reason

    assert repo.add_bank_import(
        [batch.rows[0].transaction()],
        reviewed_import_metadata(batch, [batch.rows[0]]),
    ) == 1
    repeated = build_review_batch(first_csv, "rogers.csv", repo, bank="Rogers")
    assert repeated.rows[0].locked is True
    assert repeated.rows[0].duplicate_reason == "Already imported"

    second_csv = header + (
        f'2026-07-04,2026-07-05,ref-3,TRANS,APPROVED,************8927,'
        f'"{category}",FIDO Mobile ******4890,TORONTO,ON,CAN,M4Y2Y5,$46.00,,User\n'
    )
    learned = build_review_batch(second_csv, "rogers-new.csv", repo, bank="Rogers")
    assert learned.rows[0].suggestion_source.startswith("Learned from")
    learned.rows[0].description = "Mobile plan"
    assert repo.add_bank_import(
        [learned.rows[0].transaction()],
        reviewed_import_metadata(learned, [learned.rows[0]]),
    ) == 1
    assert repo.vendor_preferences("Rogers", ["fido"])["fido"]["description"] == "Mobile plan"


def test_editing_an_imported_transaction_teaches_future_vendor_preferences(tmp_path):
    repo = repository(tmp_path)
    imported = TransactionInput(
        date(2026, 7, 4), Decimal("45.20"), "Phone bill", "Bills & Utilities",
        subcategory="Cellular", source_key="rogers-fido-edit", source_bank="Rogers",
        source_vendor="FIDO Mobile", source_vendor_key="fido",
    )
    assert repo.add_bank_import(
        [imported], single_import_metadata(imported, "rogers-edit.csv"),
    ) == 1
    identifier = repo.list("2026-07")[0]["id"]

    corrected = TransactionInput(
        date(2026, 7, 4), Decimal("45.20"), "Mobile plan", "Bills & Utilities",
        subcategory="Phone",
    )
    repo.update(identifier, corrected)
    learned = repo.vendor_preferences("Rogers", ["fido"])["fido"]
    assert learned["description"] == "Mobile plan"
    assert learned["subcategory"] == "Phone"

    # Changing an unrelated field must not inflate the learned choice.
    repo.update(
        identifier,
        TransactionInput(
            date(2026, 7, 5), Decimal("46.00"), "Mobile plan", "Bills & Utilities",
            subcategory="Phone",
        ),
    )
    assert repo.vendor_preferences("Rogers", ["fido"])["fido"]["uses"] == learned["uses"]


def test_bank_review_flags_semantic_duplicate_against_existing_expense(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(date(2026, 5, 23), Decimal("7.50"), "COBS", "Groceries"))
    csv_text = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
2,5524890045782513,20260523,20260525,7.5,COBS Bread TORONTO ON
"""
    batch = build_review_batch(csv_text, "bmo.csv", repo, bank="BMO")
    assert batch.rows[0].include is False
    assert "existing expenses" in batch.rows[0].duplicate_reason
    assert batch.rows[0].locked is False


def test_bank_review_matches_manual_vendor_even_when_suggested_description_differs(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(date(2026, 6, 7), Decimal("67.31"), "Shell", "Transportation"))
    csv_text = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
3,5524890045782513,20260607,20260609,67.31,SHELL C02146 TORONTO ON
"""
    batch = build_review_batch(csv_text, "bmo.csv", repo, bank="BMO")
    assert batch.rows[0].description == "Gas"
    assert batch.rows[0].include is False
    assert batch.rows[0].duplicate_reason == "Possible duplicate in existing expenses"


def test_selected_bank_must_match_csv_format():
    bmo_csv = """Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description
1,0000000000000000,20260810,20260811,12.00,SHOP
"""
    with pytest.raises(ValueError, match="selected Rogers"):
        parse_bank_csv(bmo_csv, "Rogers")
