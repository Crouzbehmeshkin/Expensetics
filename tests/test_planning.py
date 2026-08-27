from dataclasses import replace
from datetime import date
from decimal import Decimal
import sqlite3

import pytest

from finance_app.charts import is_over_budget
from finance_app.db import initialize
from finance_app.export import export_encrypted_backup, restore_encrypted_backup
from finance_app.models import IncomeEstimateInput, IncomeInput, LiabilityInput, TransactionInput
from finance_app.repository import Repository
from finance_app.schema import CURRENT_SCHEMA_VERSION
from finance_app.services import (
    ANNUAL_EXPENSE_TYPE, loan_payment_cents, scheduled_balance_cents,
    balance_after_payments_cents, exponential_average_cents, projected_payoff_months,
    payment_monthly_equivalent, scheduled_payments_due, weighted_income_forecast,
)
from finance_app.vault import prepare


def repository(path) -> Repository:
    database = path / "finance.db"
    initialize(database)
    return Repository(database)


def encrypted_repository(path, password: str) -> Repository:
    database = path / "finance.db"
    prepare(database, password, password)
    initialize(database)
    return Repository(database)


def test_categories_can_be_archived_without_removing_history(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(date(2026, 7, 1), Decimal("12"), "Gift", "Gifts"))
    gifts_id = next(
        item["id"] for item in repo.category_library() if item["name"] == "Gifts"
    )

    repo.set_category_active(gifts_id, False)

    assert "Gifts" not in repo.categories()
    assert repo.list("2026-07", category="Gifts")[0]["description"] == "Gift"
    settings = {row["name"]: row["is_active"] for row in repo.category_settings()}
    assert settings["Gifts"] == 0
    gifts = next(
        item for item in repo.category_trend("2026-07", count=1)["series"]
        if item["name"] == "Gifts"
    )
    assert gifts["values"] == [1200]
    assert repo.predicted_category("Gift") is None
    assert repo.suggestions("gif") == []


def test_version_eleven_liability_table_gains_current_planning_columns(tmp_path):
    database = tmp_path / "finance.db"
    initialize(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            ALTER TABLE liabilities RENAME TO liabilities_v11;
            CREATE TABLE liabilities (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, liability_type TEXT NOT NULL,
                original_principal_cents INTEGER NOT NULL, annual_rate_bps INTEGER NOT NULL,
                term_months INTEGER NOT NULL, start_date TEXT NOT NULL,
                payment_cents INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO liabilities (
                id, name, liability_type, original_principal_cents, annual_rate_bps,
                term_months, start_date, payment_cents, notes
            ) VALUES (1, 'Existing loan', 'Other', 100000, 0, 12, '2026-01-01', 8333, '');
            DROP TABLE liabilities_v11;
            UPDATE schema_version SET version=11;
            """
        )

    initialize(database)
    loan = Repository(database).liabilities(as_of=date(2026, 1, 1))[0]
    assert loan["payment_match_key"] == ""
    assert loan["payment_match_label"] == ""
    assert loan["rate_type"] == "Fixed"
    assert loan["interest_convention"] == "Monthly"
    assert loan["rate_term_months"] == 60
    assert loan["current_balance_cents"] == 100000
    assert loan["balance_as_of_date"] == "2025-12-31"
    assert loan["payment_frequency"] == "Monthly"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_at_least_one_active_category_is_required(tmp_path):
    repo = repository(tmp_path)
    active = [item for item in repo.category_library() if item["is_active"]]
    for category in active[:-1]:
        repo.set_category_active(category["id"], False)

    with pytest.raises(ValueError, match="at least one"):
        repo.set_category_active(active[-1]["id"], False)


def test_reserved_filter_labels_cannot_become_definitions(tmp_path):
    repo = repository(tmp_path)

    with pytest.raises(ValueError, match="reserved"):
        repo.add_category("All")
    groceries_id = next(
        item["id"] for item in repo.category_library() if item["name"] == "Groceries"
    )
    with pytest.raises(ValueError, match="different subcategory"):
        repo.add_subcategory(groceries_id, "__all__")


def test_custom_category_library_preserves_used_names_and_subcategories(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 1), Decimal("25"), "Market", "Groceries",
        subcategory="Fresh food",
    ))

    category_id = repo.add_category("Home projects", ["Materials", "Tools"])
    repo.add_subcategory(category_id, "Contractors")
    repo.move_category(category_id, -1)

    custom = next(
        category for category in repo.category_library()
        if category["name"] == "Home projects"
    )
    assert [item["name"] for item in custom["subcategories"]] == [
        "Materials", "Tools", "Contractors",
    ]
    assert repo.subcategory_options("Home projects") == [
        "Materials", "Tools", "Contractors",
    ]

    groceries = next(
        category for category in repo.category_library()
        if category["name"] == "Groceries"
    )
    replacement = repo.replace_category_name(groceries["id"], "Food at home")

    assert replacement["history_preserved"] is True
    assert "Food at home" in repo.categories()
    assert "Groceries" not in repo.categories()
    assert repo.list("2026-07", category="Groceries")[0]["description"] == "Market"
    archived = next(
        category for category in repo.category_library()
        if category["name"] == "Groceries"
    )
    assert archived["is_active"] == 0


def test_category_mapping_is_previewed_audited_and_rerunnable(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 1), Decimal("80"), "Costco", "Groceries",
        subcategory="Warehouse",
    ))
    repo.add(TransactionInput(
        date(2026, 7, 2), Decimal("20"), "Market", "Groceries",
        subcategory="Fresh",
    ))
    target_id = repo.add_category("Food at home", ["Bulk"])
    library = repo.category_library()
    source_id = next(item["id"] for item in library if item["name"] == "Groceries")
    bulk = next(
        item for category in library if category["id"] == target_id
        for item in category["subcategories"] if item["name"] == "Bulk"
    )
    repo.set_subcategory_active(bulk["id"], False)

    preview = repo.category_migration_preview(
        source_id, target_id, source_subcategory="Warehouse",
        target_subcategory_action="replace", target_subcategory="Bulk",
    )
    assert preview["transaction_count"] == 1
    assert preview["amount_cents"] == 8000
    assert preview["first_date"] == preview["last_date"] == "2026-07-01"

    applied = repo.apply_category_migration(
        source_id, target_id, source_subcategory="Warehouse",
        target_subcategory_action="replace", target_subcategory="Bulk",
    )
    assert applied["migration_id"] > 0
    mapped = repo.list("2026-07", category="Food at home")
    assert [(row["description"], row["subcategory"]) for row in mapped] == [
        ("Costco", "Bulk"),
    ]
    assert repo.subcategory_options("Food at home") == ["Bulk"]
    assert repo.list("2026-07", category="Groceries")[0]["description"] == "Market"

    repo.apply_category_migration(
        source_id, target_id, target_subcategory_action="keep",
    )
    mapped = repo.list("2026-07", category="Food at home")
    assert {row["subcategory"] for row in mapped} == {"Bulk", "Fresh"}
    history = repo.category_migration_history()
    assert [row["affected_transactions"] for row in history[:2]] == [1, 1]


def test_category_mapping_rejects_noop_and_leaves_budgets_separate(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 1), Decimal("15"), "Market", "Groceries",
    ))
    repo.save_budgets("2026-07", {"Groceries": "300"})
    source_id = next(
        item["id"] for item in repo.category_library() if item["name"] == "Groceries"
    )

    with pytest.raises(ValueError, match="changes"):
        repo.apply_category_migration(source_id, source_id)

    target_id = repo.add_category("Food")
    repo.apply_category_migration(source_id, target_id)
    with sqlite3.connect(repo.db_path) as connection:
        groceries_budget = connection.execute(
            """SELECT amount_cents FROM monthly_budgets b
               JOIN categories c ON c.id=b.category_id WHERE c.name='Groceries'"""
        ).fetchone()[0]
    assert groceries_budget == 30000


def test_renaming_a_category_without_transactions_keeps_linked_plans(tmp_path):
    repo = repository(tmp_path)
    repo.save_budgets("2026-07", {"Groceries": "300"})
    source = next(
        item for item in repo.category_library() if item["name"] == "Groceries"
    )

    renamed = repo.replace_category_name(source["id"], "Food at home")

    assert renamed == {
        "id": source["id"], "source_id": source["id"], "history_preserved": False,
    }
    budget = next(
        item for item in repo.budgets("2026-07")
        if item["category"] == "Food at home"
    )
    assert budget["amount_cents"] == 30000


def test_case_only_category_rename_does_not_split_history(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 1), Decimal("15"), "Market", "Groceries",
    ))
    source = next(
        item for item in repo.category_library() if item["name"] == "Groceries"
    )

    renamed = repo.replace_category_name(source["id"], "groceries")

    assert renamed["id"] == source["id"]
    assert renamed["history_preserved"] is False
    assert repo.list("2026-07", category="groceries")[0]["description"] == "Market"
    assert not any(
        item["name"] == "Groceries" for item in repo.category_library()
    )


def test_current_schema_catalogs_existing_historical_subcategories(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 1), Decimal("15"), "Market", "Groceries",
        subcategory="Produce",
    ))
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute("DROP TABLE category_migration_log")
        connection.execute("DROP TABLE category_subcategories")
        connection.execute("UPDATE schema_version SET version=15")

    initialize(repo.db_path)

    assert repo.subcategory_options("Groceries") == ["Produce"]
    with sqlite3.connect(repo.db_path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_budget_uses_monthly_equivalent_and_persists_until_revised(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 7, 3), Decimal("120"), "Annual membership", "Shopping",
        expense_type=ANNUAL_EXPENSE_TYPE,
    ))
    repo.save_budgets("2026-08", {"Shopping": "25.00", "Groceries": "300"})
    shopping = next(row for row in repo.budgets("2026-08") if row["category"] == "Shopping")
    assert shopping["amount_cents"] == 2500
    assert shopping["actual_cents"] == 1000
    assert shopping["remaining_cents"] == 1500
    assert next(row for row in repo.budgets("2025-02") if row["category"] == "Groceries")["amount_cents"] == 30000
    assert next(row for row in repo.budgets("2026-09") if row["category"] == "Shopping")["amount_cents"] == 2500

    repo.save_budgets("2026-09", {"Shopping": "40", "Groceries": "350"})
    assert next(row for row in repo.budgets("2026-08") if row["category"] == "Shopping")["amount_cents"] == 2500
    assert next(row for row in repo.budgets("2026-10") if row["category"] == "Shopping")["amount_cents"] == 4000


def test_budget_indicator_recomputes_after_budget_revision(tmp_path):
    repo = repository(tmp_path)
    repo.add(TransactionInput(
        date(2026, 8, 3), Decimal("1200"), "Laptop", "Shopping",
        expense_type=ANNUAL_EXPENSE_TYPE,
    ))
    repo.save_budgets("2026-08", {"Shopping": "80"})
    shopping = next(
        row for row in repo.budgets("2026-08") if row["category"] == "Shopping"
    )
    assert is_over_budget(shopping["actual_cents"], shopping["amount_cents"])

    repo.save_budgets("2026-08", {"Shopping": "150"})
    shopping = next(
        row for row in repo.budgets("2026-08") if row["category"] == "Shopping"
    )
    assert shopping["actual_cents"] == 10_000
    assert shopping["remaining_cents"] == 5_000
    assert not is_over_budget(shopping["actual_cents"], shopping["amount_cents"])


def test_budget_revision_can_apply_to_all_time_or_one_calendar_year(tmp_path):
    repo = repository(tmp_path)
    repo.save_budgets("2026-03", {"Groceries": "300"})
    repo.save_budgets("2027-01", {"Groceries": "450"})
    repo.save_budgets("2026-08", {"Groceries": "375"}, scope="year")
    assert next(row for row in repo.budgets("2026-01") if row["category"] == "Groceries")["amount_cents"] == 37500
    assert next(row for row in repo.budgets("2026-12") if row["category"] == "Groceries")["amount_cents"] == 37500
    assert next(row for row in repo.budgets("2027-01") if row["category"] == "Groceries")["amount_cents"] == 45000

    repo.save_budgets("2026-08", {"Groceries": "500"}, scope="all_time")
    assert next(row for row in repo.budgets("2020-01") if row["category"] == "Groceries")["amount_cents"] == 50000
    assert next(row for row in repo.budgets("2030-01") if row["category"] == "Groceries")["amount_cents"] == 50000


def test_zero_budget_revision_clears_an_earlier_budget_without_changing_history(tmp_path):
    repo = repository(tmp_path)
    repo.save_budgets("2026-01", {"Groceries": "300", "Shopping": "100"})
    repo.save_budgets("2026-09", {"Groceries": "0", "Shopping": "125"})

    assert next(
        row for row in repo.budgets("2026-08") if row["category"] == "Groceries"
    )["amount_cents"] == 30_000
    assert next(
        row for row in repo.budgets("2026-09") if row["category"] == "Groceries"
    )["amount_cents"] == 0
    assert next(
        row for row in repo.budgets("2027-01") if row["category"] == "Groceries"
    )["amount_cents"] == 0
    assert next(
        row for row in repo.budgets("2027-01") if row["category"] == "Shopping"
    )["amount_cents"] == 12_500


def test_zero_year_budget_restores_the_previous_future_plan(tmp_path):
    repo = repository(tmp_path)
    repo.save_budgets("2025-01", {"Groceries": "300"})
    repo.save_budgets("2027-01", {"Groceries": "450"})
    repo.save_budgets("2026-06", {"Groceries": "0"}, scope="year")

    assert next(
        row for row in repo.budgets("2026-12") if row["category"] == "Groceries"
    )["amount_cents"] == 0
    assert next(
        row for row in repo.budgets("2027-01") if row["category"] == "Groceries"
    )["amount_cents"] == 45_000


def test_income_forecast_is_weighted_regression_in_integer_cents():
    forecast = weighted_income_forecast([
        ("2026-04", 100_000), ("2026-05", 110_000), ("2026-06", 120_000),
    ])
    assert forecast.amount_cents == 130_000
    assert forecast.method == "exponentially_weighted_regression"
    assert forecast.source_months == ("2026-04", "2026-05", "2026-06")
    gap_forecast = weighted_income_forecast(
        [("2026-05", 100_000), ("2026-06", 110_000)], target_month="2026-08",
    )
    assert gap_forecast.amount_cents == 130_000


def test_income_estimate_uses_only_prior_recorded_months_and_override_is_not_cashflow(tmp_path):
    repo = repository(tmp_path)
    repo.add_income(IncomeInput(date(2026, 5, 1), Decimal("1000"), "Salary"))
    repo.add_income(IncomeInput(date(2026, 6, 1), Decimal("1100"), "Salary"))
    repo.add_income(IncomeInput(date(2026, 7, 1), Decimal("1200"), "Salary"))

    estimate = repo.income_estimate("2026-08")
    assert estimate["amount_cents"] == 130_000
    assert estimate["source_months"] == ("2026-05", "2026-06", "2026-07")

    repo.save_income_estimate(IncomeEstimateInput("2026-08", Decimal("1450")))
    dashboard = repo.dashboard("2026-08")
    assert dashboard["income_estimate"]["amount_cents"] == 145_000
    assert dashboard["income_estimate"]["is_override"] is True
    assert dashboard["income"] == 0
    assert dashboard["net_cashflow"] == 0
    assert dashboard["display_income"] == 145_000
    assert dashboard["display_net_cashflow"] == 145_000
    assert dashboard["income_is_estimated"] is True

    repo.add_income(IncomeInput(date(2026, 8, 1), Decimal("1400"), "Salary"))
    recorded = repo.dashboard("2026-08")
    assert recorded["display_income"] == 140_000
    assert recorded["display_net_cashflow"] == 140_000
    assert recorded["income_is_estimated"] is False


def test_loan_calculation_and_scheduled_balance_are_deterministic(tmp_path):
    assert loan_payment_cents(300_000_00, 500, 360) == 161_046
    assert loan_payment_cents(300_000_00, 500, 300, "Canadian semi-annual") == 174_481
    assert scheduled_balance_cents(120_000, 0, 12, 6) == 60_000
    assert scheduled_balance_cents(120_000, 0, 12, 12) == 0
    assert scheduled_payments_due(date(2026, 1, 31), date(2026, 2, 27)) == 1
    assert scheduled_payments_due(date(2026, 1, 31), date(2026, 2, 28)) == 2

    repo = repository(tmp_path)
    identifier = repo.save_liability(LiabilityInput(
        name="Home", liability_type="Mortgage",
        original_principal=Decimal("300000"), annual_rate_percent=Decimal("5"),
        term_months=360, start_date=date(2026, 1, 1),
    ))
    loan = repo.liabilities(as_of=date(2026, 8, 1))[0]
    assert loan["id"] == identifier
    assert loan["payment_cents"] == 161_046
    assert loan["payments_made"] == 8
    assert 29_700_000 < loan["scheduled_balance_cents"] < 30_000_000


def test_liability_projection_starts_at_recorded_balance_instead_of_repricing_history(tmp_path):
    repo = repository(tmp_path)
    anchor = date(2026, 8, 1)
    item = LiabilityInput(
        name="Renewed home mortgage", liability_type="Mortgage",
        original_principal=Decimal("300000"), current_balance=Decimal("256423.59"),
        balance_as_of=anchor, annual_rate_percent=Decimal("5"),
        term_months=240, start_date=date(2021, 8, 1),
        interest_convention="Canadian semi-annual", rate_term_months=60,
    )
    identifier = repo.save_liability(item)

    at_anchor = repo.liabilities(as_of=anchor)[0]
    assert at_anchor["scheduled_balance_cents"] == 25_642_359
    assert at_anchor["estimated_balance_cents"] == 25_642_359
    assert at_anchor["payments_made"] == 0

    repo.save_liability(replace(item, annual_rate_percent=Decimal("6")), identifier)
    repriced = repo.liabilities(as_of=anchor)[0]
    assert repriced["scheduled_balance_cents"] == 25_642_359


def test_payment_frequency_is_explicit_and_has_a_deterministic_monthly_equivalent():
    monthly = loan_payment_cents(
        300_000_00, 500, 300, "Canadian semi-annual", "Monthly",
    )
    accelerated = loan_payment_cents(
        300_000_00, 500, 300, "Canadian semi-annual", "Accelerated biweekly",
    )
    assert accelerated == 87_241
    assert accelerated == (monthly + 1) // 2
    assert payment_monthly_equivalent(accelerated, "Accelerated biweekly") == 189_022


def test_matched_imported_payments_drive_auditable_payoff_projection(tmp_path):
    repo = repository(tmp_path)
    for index, month in enumerate((5, 6, 7), start=1):
        repo.add(TransactionInput(
            date(2026, month, 1), Decimal("2000"), "Mortgage payment", "Housing",
            source_key=f"bank-payment-{index}", source_bank="Bank",
            source_vendor="HOME LOAN PAYMENT", source_vendor_key="home loan payment",
        ))
    candidates = repo.recurring_payment_candidates()
    candidate = next(item for item in candidates if item["match_key"] == "vendor:home loan payment")
    assert candidate["uses"] == 3
    assert candidate["average_cents"] == 200_000

    repo.save_liability(LiabilityInput(
        "Home", "Mortgage", Decimal("300000"), Decimal("5"), 360,
        date(2026, 5, 1), payment_match_key=candidate["match_key"],
        payment_match_label=candidate["label"],
    ))
    loan = repo.liabilities(as_of=date(2026, 7, 31))[0]
    assert loan["observed_months"] == 3
    assert loan["observed_payment_cents"] == 200_000
    assert loan["estimated_balance_cents"] < loan["scheduled_balance_cents"]
    assert loan["projected_payoff_months"] < loan["term_months"] - loan["payments_made"]


def test_liability_insights_separate_observed_and_contractual_payments(tmp_path):
    repo = repository(tmp_path)
    repo.save_liability(LiabilityInput(
        "Home", "Mortgage", Decimal("300000"), Decimal("5"), 360,
        date(2026, 4, 1), payment_match_key="vendor:home loan payment",
        payment_match_label="HOME LOAN PAYMENT",
    ))
    for index, month in enumerate((5, 6, 7), start=1):
        repo.add(TransactionInput(
            date(2026, month, 1), Decimal("2000"), "Mortgage payment", "Housing",
            source_key=f"insight-payment-{index}", source_bank="Bank",
            source_vendor="HOME LOAN PAYMENT", source_vendor_key="home loan payment",
        ))

    insights = repo.liability_insights("2026-08", count=5)

    assert insights["months"] == [
        "2026-04", "2026-05", "2026-06", "2026-07", "2026-08",
    ]
    assert insights["observed_payments"] == [0, 200_000, 200_000, 200_000, 0]
    assert insights["scheduled_payments"][0] > 0
    assert insights["scheduled_payments"][-1] > 0
    assert insights["total_balance_cents"] == insights["loans"][0]["balance_cents"]
    assert insights["total_repaid_cents"] > 0
    assert insights["paydown_pace_cents"] > 0
    assert insights["loans"][0]["payment_source"] == "observed"
    assert len(insights["balance_series"][0]["values"]) == 5


def test_liability_insights_are_empty_without_active_liabilities(tmp_path):
    insights = repository(tmp_path).liability_insights("2026-08", count=3)
    assert insights["loans"] == []
    assert insights["total_balance_cents"] == 0
    assert insights["observed_payments"] == [0, 0, 0]


def test_budget_trend_follows_effective_revisions_for_total_and_category(tmp_path):
    repo = repository(tmp_path)
    repo.save_budgets("2026-07", {"Groceries": "300", "Shopping": "100"})
    repo.save_budgets("2026-09", {"Groceries": "500"})

    total = repo.budget_trend("2026-09", count=3)
    groceries = repo.budget_trend("2026-09", count=3, category="Groceries")

    assert total == {
        "months": ["2026-07", "2026-08", "2026-09"],
        "values": [40_000, 40_000, 60_000],
    }
    assert groceries["values"] == [30_000, 30_000, 50_000]


def test_total_budget_line_requires_an_explicit_overall_limit(tmp_path):
    repo = repository(tmp_path)
    repo.save_budgets("2026-07", {"Groceries": "300", "Shopping": "100"})

    assert repo.total_budget_trend("2026-08", count=2)["values"] == [None, None]

    repo.save_budgets(
        "2026-08", {"Groceries": "300", "Shopping": "100"},
        total_amount="2000",
    )
    assert repo.total_monthly_budget() == 200_000
    assert repo.total_budget_trend("2026-08", count=2)["values"] == [200_000, 200_000]

    repo.save_budgets(
        "2026-08", {"Groceries": "300", "Shopping": "100"},
        total_amount="",
    )
    assert repo.total_monthly_budget() is None


def test_payoff_helpers_round_interest_month_by_month():
    assert exponential_average_cents([100_000, 120_000]) == 111_765
    assert balance_after_payments_cents(100_000, 0, [30_000, 30_000]) == 40_000
    assert projected_payoff_months(100_000, 0, 25_000) == 4
    assert projected_payoff_months(100_000, 1200, 1000) is None


def test_planning_records_round_trip_through_encrypted_backup(tmp_path):
    source_dir = tmp_path / "source"
    source = encrypted_repository(source_dir, "source vault password")
    source.save_budgets("2026-08", {"Groceries": "500"})
    source.save_income_estimate(IncomeEstimateInput("2026-08", Decimal("4000")))
    source.save_liability(LiabilityInput(
        "Car", "Auto loan", Decimal("25000"), Decimal("6.5"), 60,
        date(2026, 1, 1), "No prepayments", rate_type="Variable · fixed payment",
        interest_convention="Canadian semi-annual", rate_term_months=36,
    ))

    backup = export_encrypted_backup(
        tmp_path / "planning.expensetics", "backup export password", source.db_path,
    )
    target = encrypted_repository(tmp_path / "target", "target vault password")
    restore_encrypted_backup(backup, "backup export password", target.db_path)
    assert next(row for row in target.budgets("2026-08") if row["category"] == "Groceries")["amount_cents"] == 50_000
    assert target.income_estimate("2026-08")["amount_cents"] == 400_000
    restored_loan = target.liabilities()[0]
    assert restored_loan["name"] == "Car"
    assert restored_loan["rate_type"] == "Variable · fixed payment"
    assert restored_loan["interest_convention"] == "Canadian semi-annual"
    assert restored_loan["rate_term_months"] == 36
