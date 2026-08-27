from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from fractions import Fraction

from .services import EXPENSE_KIND, SETTLEMENT_KIND, normalize_description


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
RECURRING_LOOKBACK_MONTHS = 6


def _median(values: Iterable[int]) -> Fraction:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("A median needs at least one value")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle])
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def _nearest_integer(value: Fraction) -> int:
    if value >= 0:
        return (2 * value.numerator + value.denominator) // (2 * value.denominator)
    return -_nearest_integer(-value)


def modified_z_score(value: int, history: list[int]) -> float | None:
    """Return the robust modified z-score recommended for outlier labeling.

    At least five prior observations and non-zero median absolute deviation are
    required. Degenerate histories are left unlabeled rather than guessed.
    """
    if len(history) < 5:
        return None
    center = _median(history)
    deviations = [abs(Fraction(item) - center) for item in history]
    mad = _median(deviations)
    if mad == 0:
        return None
    return float(Fraction(6745, 10_000) * (Fraction(value) - center) / mad)


def _merchant_identity(row: Mapping[str, object]) -> tuple[str, str]:
    vendor_key = str(row.get("source_vendor_key") or "").strip()
    description = str(row.get("description") or "").strip()
    key = normalize_description(vendor_key or description)
    label = str(row.get("source_vendor") or "").strip() or description
    return key, label


def _circular_day_delta(current_day: int, expected_day: int) -> int:
    """Compare month-day positions without treating Jan 31 -> Feb 1 as 30 days."""
    delta = current_day - expected_day
    return ((delta + 15) % 31) - 15


def build_transaction_insights(
    rows: Iterable[Mapping[str, object]],
    months: list[str],
    selected_category: str,
) -> dict:
    """Derive sparse, explainable observations from raw dated transactions."""
    if not months:
        raise ValueError("Insights need at least one month")
    records = []
    for source in rows:
        record = dict(source)
        record["parsed_date"] = date.fromisoformat(str(record["date"]))
        record["month"] = str(record["date"])[:7]
        records.append(record)

    current_month = months[-1]
    prior_months = months[max(0, len(months) - RECURRING_LOOKBACK_MONTHS - 1):-1]
    recorded_months = {str(row["month"]) for row in records}
    active_prior_months = [month for month in prior_months if month in recorded_months]
    current = [row for row in records if row["month"] == current_month]
    purchases = [row for row in records if row["transaction_kind"] == EXPENSE_KIND]
    settlements = [row for row in records if row["transaction_kind"] == SETTLEMENT_KIND]

    category_order: dict[str, int] = {}
    category_counts: dict[str, int] = defaultdict(int)
    for row in current:
        category = str(row["category"])
        category_order.setdefault(category, int(row.get("sort_order") or 0))
        if row["transaction_kind"] == EXPENSE_KIND:
            category_counts[category] += 1
    count_rows = [
        {"category": category, "count": count}
        for category, count in sorted(
            category_counts.items(),
            key=lambda item: (category_order.get(item[0], 0), item[0].casefold()),
        )
    ]

    selected = [row for row in records if row["category"] == selected_category]
    selected_current = [row for row in selected if row["month"] == current_month]
    selected_purchases = [
        row for row in selected if row["transaction_kind"] == EXPENSE_KIND
    ]
    selected_current_purchases = [
        row for row in selected_current if row["transaction_kind"] == EXPENSE_KIND
    ]
    selected_current_settlements = [
        row for row in selected_current if row["transaction_kind"] == SETTLEMENT_KIND
    ]
    activity = {
        "months": list(months),
        "counts": [
            sum(row["month"] == month for row in selected_purchases)
            for month in months
        ],
        "totals": [
            sum(int(row["amount_cents"]) for row in selected if row["month"] == month)
            for month in months
        ],
    }
    weekday_counts = [
        sum(row["parsed_date"].weekday() == weekday for row in selected_purchases)
        for weekday in range(7)
    ]

    settlement_activity = {
        "months": list(months),
        "counts": [
            sum(row["month"] == month for row in settlements) for month in months
        ],
        "totals": [
            -sum(
                int(row["amount_cents"])
                for row in settlements if row["month"] == month
            )
            for month in months
        ],
    }
    settlement_categories: dict[str, dict] = {}
    for row in settlements:
        if row["month"] != current_month:
            continue
        item = settlement_categories.setdefault(
            str(row["category"]), {"category": str(row["category"]), "count": 0, "total": 0},
        )
        item["count"] += 1
        item["total"] -= int(row["amount_cents"])
    settlement_activity["by_category"] = sorted(
        settlement_categories.values(),
        key=lambda item: (-item["total"], item["category"].casefold()),
    )

    merchants: dict[str, dict] = {}
    for row in purchases:
        key, label = _merchant_identity(row)
        if not key:
            continue
        merchant = merchants.setdefault(key, {"label": label, "transactions": []})
        merchant["label"] = label  # rows are date-ordered, so retain the latest display form
        merchant["transactions"].append(row)

    signals: list[dict] = []
    recurring_candidates = 0
    stable_recurring = 0
    recurring_changes: list[dict] = []
    timing_changes: list[dict] = []
    for key, merchant in merchants.items():
        by_month: dict[str, list[dict]] = defaultdict(list)
        for row in merchant["transactions"]:
            by_month[row["month"]].append(row)
        prior_active = [by_month[month] for month in prior_months if by_month.get(month)]
        current_rows = by_month.get(current_month, [])
        if (
            len(prior_active) < 4
            or len(current_rows) != 1
            or any(len(entries) != 1 for entries in prior_active)
        ):
            continue
        recurring_candidates += 1
        current_row = current_rows[0]
        current_amount = int(current_row["amount_cents"])
        usual_amount = _median([int(entries[0]["amount_cents"]) for entries in prior_active])
        current_day = current_row["parsed_date"].day
        expected_day = _nearest_integer(
            _median([entries[0]["parsed_date"].day for entries in prior_active])
        )
        day_delta = _circular_day_delta(current_day, expected_day)
        increase = Fraction(current_amount) - usual_amount
        increase_threshold = max(Fraction(500), usual_amount / 10)
        amount_close = abs(increase) <= max(Fraction(200), usual_amount / 20)
        timing_close = abs(day_delta) <= 3
        if amount_close and timing_close:
            stable_recurring += 1
        if increase >= increase_threshold:
            recurring_changes.append({
                "kind": "recurring_increase",
                "priority": 10,
                "merchant_key": key,
                "label": merchant["label"],
                "category": current_row["category"],
                "current_cents": current_amount,
                "usual_cents": _nearest_integer(usual_amount),
                "change_cents": _nearest_integer(increase),
                "percent": _nearest_integer(increase * 100 / usual_amount) if usual_amount else None,
            })
        if abs(day_delta) >= 5:
            timing_changes.append({
                "kind": "timing_shift",
                "priority": 30,
                "label": merchant["label"],
                "category": current_row["category"],
                "days": abs(day_delta),
                "direction": "later" if day_delta > 0 else "earlier",
                "current_day": current_day,
                "expected_day": expected_day,
            })
    recurring_changes.sort(key=lambda item: (-item["change_cents"], item["label"].casefold()))
    timing_changes.sort(key=lambda item: (-item["days"], item["label"].casefold()))

    outliers = []
    outlier_keys: set[str] = set()
    for key, merchant in merchants.items():
        history = [
            int(row["amount_cents"])
            for row in merchant["transactions"] if row["month"] != current_month
        ]
        if len(history) < 7:
            continue
        center = _median(history)
        material_difference = max(Fraction(1_000), abs(center) / 2)
        for row in merchant["transactions"]:
            if row["month"] != current_month:
                continue
            amount = int(row["amount_cents"])
            score = modified_z_score(amount, history)
            if score is None or abs(score) <= 3.5 or abs(Fraction(amount) - center) < material_difference:
                continue
            outlier_keys.add(key)
            outliers.append({
                "kind": "amount_outlier",
                "priority": 20,
                "label": merchant["label"],
                "category": row["category"],
                "amount_cents": amount,
                "usual_cents": _nearest_integer(center),
                "direction": "higher" if amount > center else "lower",
                "score": abs(score),
            })
    outliers.sort(key=lambda item: (-item["score"], item["label"].casefold()))
    recurring_changes = [
        item for item in recurring_changes if item["merchant_key"] not in outlier_keys
    ]
    for item in recurring_changes:
        item.pop("merchant_key")
    signals.extend(recurring_changes[:2])
    signals.extend(outliers[:2])
    signals.extend(timing_changes[:2])

    activity_changes = []
    for merchant in merchants.values():
        monthly: dict[str, int] = defaultdict(int)
        for row in merchant["transactions"]:
            monthly[row["month"]] += 1
        previous = [monthly.get(month, 0) for month in active_prior_months]
        current_count = monthly.get(current_month, 0)
        if sum(value > 0 for value in previous) < 2:
            continue
        baseline = _median(previous)
        previous_month_count = previous[-1]
        if (
            current_count > previous_month_count
            and current_count >= 3
            and current_count >= baseline + 2
            and current_count >= max(2, baseline * 2)
        ):
            current_category = next(
                str(row["category"])
                for row in reversed(merchant["transactions"])
                if row["month"] == current_month
            )
            activity_changes.append({
                "kind": "merchant_activity",
                "priority": 40,
                "label": merchant["label"],
                "category": current_category,
                "current_count": current_count,
                "usual_count": _nearest_integer(baseline),
                "change_count": _nearest_integer(Fraction(current_count) - baseline),
            })
    activity_changes.sort(
        key=lambda item: (-item["change_count"], item["label"].casefold()),
    )
    signals.extend(activity_changes[:1])

    category_monthly: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in purchases:
        category_monthly[str(row["category"])][row["month"]] += 1
    category_activity_changes = []
    for category, monthly in category_monthly.items():
        previous_category_counts = [
            monthly.get(month, 0) for month in active_prior_months
        ]
        current_category_count = monthly.get(current_month, 0)
        if sum(value > 0 for value in previous_category_counts) < 2:
            continue
        baseline = _median(previous_category_counts)
        previous_month_count = previous_category_counts[-1]
        if (
            current_category_count > previous_month_count
            and current_category_count >= 3
            and current_category_count >= baseline + 2
            and current_category_count >= baseline * Fraction(3, 2)
        ):
            category_activity_changes.append({
                "kind": "category_activity",
                "priority": 45,
                "category": category,
                "current_count": current_category_count,
                "usual_count": _nearest_integer(baseline),
                "change_count": _nearest_integer(Fraction(current_category_count) - baseline),
            })
    category_activity_changes.sort(
        key=lambda item: (-item["change_count"], item["category"].casefold()),
    )
    signals.extend(category_activity_changes[:1])

    if recurring_candidates >= 2 and stable_recurring == recurring_candidates:
        signals.append({
            "kind": "recurring_stable",
            "priority": 60,
            "stable_count": stable_recurring,
            "total_count": recurring_candidates,
        })
    current_settlement_total = sum(item["total"] for item in settlement_activity["by_category"])
    current_settlement_count = sum(item["count"] for item in settlement_activity["by_category"])
    if current_settlement_count:
        signals.append({
            "kind": "settlement_activity",
            "priority": 70,
            "count": current_settlement_count,
            "total_cents": current_settlement_total,
        })

    signals.sort(key=lambda item: (item["priority"], item.get("label", "").casefold()))
    for signal in signals:
        signal.pop("priority", None)

    purchase_total = sum(int(row["amount_cents"]) for row in selected_current_purchases)
    return {
        "month": current_month,
        "category": selected_category,
        "category_counts": count_rows,
        "selected": {
            "net_total_cents": sum(int(row["amount_cents"]) for row in selected_current),
            "purchase_total_cents": purchase_total,
            "purchase_count": len(selected_current_purchases),
            "settlement_count": len(selected_current_settlements),
            "average_purchase_cents": (
                _nearest_integer(Fraction(purchase_total, len(selected_current_purchases)))
                if selected_current_purchases else 0
            ),
            "active_days": len({row["date"] for row in selected_current_purchases}),
        },
        "activity": activity,
        "weekday": {"labels": list(WEEKDAYS), "counts": weekday_counts},
        "settlements": settlement_activity,
        "signals": signals[:6],
        "recurring_candidates": recurring_candidates,
    }
