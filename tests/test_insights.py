from finance_app.insights import build_transaction_insights, modified_z_score
from finance_app.services import EXPENSE_KIND, SETTLEMENT_KIND


MONTHS = [f"2026-{month:02d}" for month in range(1, 8)]


def row(
    date_value: str,
    amount: int,
    description: str,
    category: str,
    *,
    kind: str = EXPENSE_KIND,
    vendor_key: str = "",
) -> dict:
    return {
        "date": date_value,
        "amount_cents": amount,
        "transaction_kind": kind,
        "description": description,
        "source_vendor": description,
        "source_vendor_key": vendor_key,
        "category": category,
        "sort_order": 1,
    }


def test_modified_z_score_requires_robust_history() -> None:
    assert modified_z_score(500, [98, 99, 100, 101]) is None
    assert modified_z_score(500, [100, 100, 100, 100, 100]) is None
    assert modified_z_score(500, [98, 99, 100, 101, 102, 103, 104]) > 3.5


def test_transaction_insights_find_material_changes_without_counting_settlements_as_visits() -> None:
    records = []
    for month in range(1, 7):
        records.append(row(f"2026-{month:02d}-05", 1_000, "Stream Co", "Bills", vendor_key="stream"))
        records.append(row(f"2026-{month:02d}-12", 150_000, "Rent", "Housing", vendor_key="rent"))
    records.extend([
        row("2026-07-13", 1_500, "Stream Co", "Bills", vendor_key="stream"),
        row("2026-07-12", 150_000, "Rent", "Housing", vendor_key="rent"),
    ])

    for month in (1, 2, 3):
        records.append(row(f"2026-{month:02d}-10", 1_200, "Cafe", "Dining", vendor_key="cafe"))
    records.extend([
        row("2026-07-04", 1_100, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-07-11", 1_300, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-07-18", 1_250, "Cafe", "Dining", vendor_key="cafe"),
    ])

    for index, amount in enumerate((4_900, 5_000, 5_100, 4_950, 5_050, 5_000, 5_100)):
        month = index % 6 + 1
        records.append(row(
            f"2026-{month:02d}-{20 + index // 6:02d}", amount,
            "Fuel Stop", "Transportation", vendor_key="fuel",
        ))
    records.append(row("2026-07-20", 20_000, "Fuel Stop", "Transportation", vendor_key="fuel"))
    records.append(row(
        "2026-07-21", -3_000, "Trip share", "Travel",
        kind=SETTLEMENT_KIND, vendor_key="trip-share",
    ))

    result = build_transaction_insights(records, MONTHS, "Dining")
    kinds = {signal["kind"] for signal in result["signals"]}
    assert {"recurring_increase", "timing_shift", "amount_outlier"} <= kinds
    assert "merchant_activity" in kinds or "category_activity" in kinds
    assert result["selected"]["purchase_count"] == 3
    assert result["selected"]["average_purchase_cents"] == 1_217
    assert result["selected"]["settlement_count"] == 0
    assert next(item for item in result["category_counts"] if item["category"] == "Dining")["count"] == 3
    assert result["settlements"]["counts"][-1] == 1
    assert result["settlements"]["totals"][-1] == 3_000
    assert result["settlements"]["by_category"] == [
        {"category": "Travel", "count": 1, "total": 3_000},
    ]
    assert build_transaction_insights(records, MONTHS, "Housing")["signals"] == result["signals"]


def test_robust_outlier_takes_precedence_over_recurring_price_change() -> None:
    records = [
        row(f"2026-{month:02d}-05", amount, "Fuel Stop", "Transportation", vendor_key="fuel")
        for month, amount in enumerate(
            (4_900, 5_000, 5_100, 4_950, 5_050, 5_000), start=1,
        )
    ]
    # A seventh observation before the recurring six-month window makes the
    # robust rule eligible without changing the one-charge-per-month baseline.
    records.append(row("2025-12-05", 5_100, "Fuel Stop", "Transportation", vendor_key="fuel"))
    records.append(row("2026-07-05", 20_000, "Fuel Stop", "Transportation", vendor_key="fuel"))

    result = build_transaction_insights(records, MONTHS, "Transportation")
    fuel_signals = [
        signal for signal in result["signals"] if signal.get("label") == "Fuel Stop"
    ]
    assert [signal["kind"] for signal in fuel_signals] == ["amount_outlier"]


def test_activity_signal_requires_an_increase_from_the_previous_month() -> None:
    months = [f"2026-{month:02d}" for month in range(1, 9)]
    records = [
        row(f"2026-{month:02d}-05", 6_000, "Phone Co", "Bills & Utilities", vendor_key="phone")
        for month in range(1, 7)
    ]
    for month in (7, 8):
        records.extend(
            row(
                f"2026-{month:02d}-{day:02d}", 2_000,
                "Phone Co", "Bills & Utilities", vendor_key="phone",
            )
            for day in (5, 12, 19)
        )

    signals = build_transaction_insights(
        records, months, "Bills & Utilities",
    )["signals"]

    assert not {
        "merchant_activity", "category_activity",
    } & {signal["kind"] for signal in signals}


def test_activity_baseline_excludes_months_without_any_records() -> None:
    months = [f"2026-{month:02d}" for month in range(1, 9)]
    records = [
        row("2026-02-05", 1_200, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-02-12", 1_300, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-07-05", 1_200, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-07-12", 1_300, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-08-05", 1_200, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-08-12", 1_300, "Cafe", "Dining", vendor_key="cafe"),
        row("2026-08-19", 1_250, "Cafe", "Dining", vendor_key="cafe"),
    ]

    signals = build_transaction_insights(records, months, "Dining")["signals"]

    assert not {
        "merchant_activity", "category_activity",
    } & {signal["kind"] for signal in signals}
