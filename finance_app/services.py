from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ANNUAL_EXPENSE_TYPE = "One-off"  # Stable storage value; the UI calls this "Annual expense".
ANNUAL_EXPENSE_MONTHS = 12
EXPENSE_TYPES = ("Living", "Discretionary", "Travel", ANNUAL_EXPENSE_TYPE)
EXPENSE_KIND = "Expense"
SETTLEMENT_KIND = "Settlement"
TRANSACTION_KINDS = (EXPENSE_KIND, SETTLEMENT_KIND)
NEED_WANT = ("", "Need", "Want")
LIABILITY_TYPES = ("Mortgage", "Auto loan", "Student loan", "Personal loan", "Other")
ACCOUNT_TYPES = (
    "Chequing", "Savings", "Credit card", "Line of credit", "Cash",
    "Investment", "Other",
)
INTEREST_CONVENTIONS = ("Monthly", "Canadian semi-annual")
MORTGAGE_RATE_TYPES = ("Fixed", "Variable · fixed payment", "Variable · adjustable payment")
PAYMENT_FREQUENCIES = (
    "Monthly", "Semi-monthly", "Biweekly", "Accelerated biweekly",
    "Weekly", "Accelerated weekly",
)
PAYMENTS_PER_YEAR = {
    "Monthly": 12,
    "Semi-monthly": 24,
    "Biweekly": 26,
    "Accelerated biweekly": 26,
    "Weekly": 52,
    "Accelerated weekly": 52,
}
INCOME_FORECAST_MONTHS = 6
INCOME_FORECAST_DECAY = Decimal("0.70")


@dataclass(frozen=True)
class IncomeForecast:
    amount_cents: int | None
    method: str
    observations: int
    source_months: tuple[str, ...]


def validate_month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-(?:0[1-9]|1[0-2])", value,
    ):
        raise ValueError("Enter a valid month") from None
    return value


def normalize_description(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def subcategory_selection(
    options_by_category: dict[str, list[str]],
    category: str,
    current: object = "",
    *,
    preserve_unknown: bool = False,
) -> tuple[list[str], str]:
    """Return category-scoped choices and a compatible selected value."""
    options = list(options_by_category.get(category, ()))
    value = str(current or "").strip()
    normalized = normalize_description(value)
    if normalized:
        match = next(
            (option for option in options if normalize_description(option) == normalized),
            None,
        )
        if match is not None:
            return options, match
        if preserve_unknown:
            options.append(value)
            return options, value
    return options, ""


def ranked_subcategory_options(
    category_library: list[dict],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Rank active choices by observed use and expose each category's favorite."""
    options_by_category: dict[str, list[str]] = {}
    preferred_by_category: dict[str, str] = {}
    for category in category_library:
        active = sorted(
            (
                subcategory for subcategory in category.get("subcategories", ())
                if subcategory.get("is_active")
            ),
            key=lambda item: (
                -int(item.get("transaction_count", 0)),
                int(item.get("sort_order", 0)),
                str(item.get("name", "")).casefold(),
            ),
        )
        options_by_category[category["name"]] = [item["name"] for item in active]
        preferred_by_category[category["name"]] = next(
            (
                item["name"] for item in active
                if int(item.get("transaction_count", 0)) > 0
            ),
            "",
        )
    return options_by_category, preferred_by_category


def most_used_subcategory(catalog: list[dict], category: str) -> str:
    """Choose the deterministic historical favorite within one category."""
    totals: dict[str, dict[str, object]] = {}
    for row in catalog:
        value = str(row.get("subcategory") or "").strip()
        if row.get("category") != category or not value:
            continue
        normalized = normalize_description(value)
        aggregate = totals.setdefault(
            normalized,
            {"name": value, "uses": 0, "last_used": ""},
        )
        aggregate["uses"] = int(aggregate["uses"]) + int(row.get("uses", 0))
        if str(row.get("last_used", "")) > str(aggregate["last_used"]):
            aggregate["name"] = value
            aggregate["last_used"] = str(row.get("last_used", ""))
    if not totals:
        return ""
    return str(min(
        totals.values(),
        key=lambda item: (
            -int(item["uses"]),
            -int(str(item["last_used"]).replace("-", "") or 0),
            str(item["name"]).casefold(),
        ),
    )["name"])


def ngram_similarity(left: object, right: object, size: int = 3) -> float:
    """Return a deterministic Sørensen–Dice score over character n-grams."""
    if size < 1:
        raise ValueError("N-gram size must be at least one")

    def grams(value: object) -> set[str]:
        cleaned = " ".join(
            "".join(
                character if character.isalnum() else " "
                for character in str(value or "").casefold()
            ).split()
        )
        if not cleaned:
            return set()
        width = min(size, len(cleaned))
        return {cleaned[index:index + width] for index in range(len(cleaned) - width + 1)}

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return 2 * len(left_grams & right_grams) / (len(left_grams) + len(right_grams))


def similar_subcategory(
    catalog: list[dict],
    queries: tuple[str, ...],
    *,
    category: str | None = None,
    threshold: float = 0.42,
) -> dict | None:
    """Find a sufficiently similar past description with a category-safe subcategory."""
    candidates: list[dict] = []
    for row in catalog:
        subcategory = str(row.get("subcategory") or "").strip()
        if not subcategory or (category is not None and row.get("category") != category):
            continue
        score = max(
            (ngram_similarity(query, row.get("description", "")) for query in queries),
            default=0.0,
        )
        if score >= threshold:
            candidates.append({**row, "similarity": score})
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            -float(row["similarity"]),
            -int(row.get("uses", 0)),
            -int(str(row.get("last_used", "")).replace("-", "") or 0),
            str(row.get("description", "")).casefold(),
            str(row.get("subcategory", "")).casefold(),
        ),
    )


def normalize_institution(value: str) -> str:
    """Return a conservative key for matching an account to a bank importer."""
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())
    aliases = {
        "monzobank": "monzo",
        "n26bank": "n26",
        "rabo": "rabobank",
        "rabobanknederland": "rabobank",
        "revolutbusiness": "revolut",
        "starlingbank": "starling",
        "transferwise": "wise",
        "bunqbank": "bunq",
        "americanexpressus": "americanexpress",
        "amex": "americanexpress",
        "bankofmontreal": "bmo",
        "bankofamerica": "bankofamerica",
        "capitalonebank": "capitalone",
        "citibank": "citi",
        "royalbank": "rbc",
        "royalbankofcanada": "rbc",
        "rogersbank": "rogers",
        "bankofnovascotia": "scotiabank",
        "scotia": "scotiabank",
        "tdbank": "td",
        "tdcanadatrust": "td",
        "torontodominionbank": "td",
        "usbank": "usbank",
        "mufgbizstation": "mufg",
        "mufgbank": "mufg",
        "mitsubishiufjbank": "mufg",
        "mizuhobusinessweb": "mizuho",
        "mizuhobank": "mizuho",
        "smbcdirect": "smbc",
        "sumitomomitsuibankingcorporation": "smbc",
    }
    return aliases.get(compact, compact)


def parse_amount(value: str | int | float | Decimal) -> int:
    try:
        cleaned = str(value).strip().replace(",", "").replace("$", "")
        amount = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid amount") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be greater than zero")
    return int(amount * 100)


def parse_transaction_amount(
    value: str | int | float | Decimal, transaction_kind: str,
) -> int:
    """Return the signed storage amount for an expense or settlement.

    Expense inputs must be positive. Settlement inputs may be entered as a
    positive magnitude in the UI or read back as a negative exported amount.
    """
    if transaction_kind not in TRANSACTION_KINDS:
        raise ValueError("Choose Expense or Settlement")
    try:
        cleaned = str(value).strip().replace(",", "").replace("$", "")
        amount = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid amount") from None
    if not amount.is_finite() or amount == 0:
        raise ValueError("Amount must be greater than zero")
    if transaction_kind == EXPENSE_KIND:
        if amount < 0:
            raise ValueError("Choose Settlement for a negative amount")
        return int(amount * 100)
    return -int(abs(amount) * 100)


def parse_nonnegative_amount(value: str | int | float | Decimal, field_name: str) -> int:
    try:
        amount = Decimal(str(value).strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {field_name.lower()} of zero or more") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Enter a valid {field_name.lower()} of zero or more")
    return int(amount * 100)


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("Enter a valid date") from None


def month_bounds(month: str) -> tuple[str, str]:
    validate_month(month)
    start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def shifted_month(month: str, offset: int) -> str:
    validate_month(month)
    year, number = (int(part) for part in month.split("-"))
    absolute = year * 12 + number - 1 + offset
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def allocate_cents(total_cents: int, periods: int) -> tuple[int, ...]:
    """Allocate every cent deterministically, assigning remainder cents earliest."""
    if periods < 1:
        raise ValueError("Periods must be at least one")
    sign = -1 if total_cents < 0 else 1
    quotient, remainder = divmod(abs(total_cents), periods)
    return tuple(
        sign * (quotient + (1 if index < remainder else 0))
        for index in range(periods)
    )


def weighted_income_forecast(
    monthly_totals: list[tuple[str, int]],
    *,
    decay: Decimal = INCOME_FORECAST_DECAY,
    target_month: str | None = None,
) -> IncomeForecast:
    """Forecast the next month with an exponentially weighted linear regression.

    Only explicitly recorded income months belong in ``monthly_totals``. Missing
    months are not silently interpreted as zero income. Integer cents are used at
    the boundary and Decimal is used throughout the calculation.
    """
    if not monthly_totals:
        return IncomeForecast(None, "insufficient_history", 0, ())
    if not Decimal("0") < decay <= Decimal("1"):
        raise ValueError("Decay must be greater than zero and no more than one")
    ordered = sorted((validate_month(month), int(total)) for month, total in monthly_totals)
    source_months = tuple(month for month, _ in ordered)
    if target_month is not None:
        validate_month(target_month)
    if len(ordered) == 1:
        return IncomeForecast(ordered[0][1], "single_month_baseline", 1, source_months)

    def ordinal(month: str) -> Decimal:
        year, number = (int(part) for part in month.split("-"))
        return Decimal(year * 12 + number - 1)

    latest = ordinal(ordered[-1][0])
    target = ordinal(target_month) if target_month else latest + 1
    if target <= latest:
        raise ValueError("Forecast target must follow the income history")
    points = [
        (ordinal(month), Decimal(total), decay ** int(latest - ordinal(month)))
        for month, total in ordered
    ]
    total_weight = sum((weight for _, _, weight in points), Decimal("0"))
    x_mean = sum((x * weight for x, _, weight in points), Decimal("0")) / total_weight
    y_mean = sum((y * weight for _, y, weight in points), Decimal("0")) / total_weight
    denominator = sum(
        (weight * (x - x_mean) ** 2 for x, _, weight in points), Decimal("0")
    )
    if denominator == 0:
        predicted = y_mean
        method = "exponential_average"
    else:
        slope = sum(
            (weight * (x - x_mean) * (y - y_mean) for x, y, weight in points),
            Decimal("0"),
        ) / denominator
        predicted = y_mean + slope * (target - x_mean)
        method = "exponentially_weighted_regression"
    amount = max(Decimal("0"), predicted).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return IncomeForecast(int(amount), method, len(ordered), source_months)


def loan_payment_cents(
    principal_cents: int, annual_rate_bps: int, term_months: int,
    interest_convention: str = "Monthly", payment_frequency: str = "Monthly",
) -> int:
    """Return one contractual payment for the chosen frequency."""
    if principal_cents <= 0:
        raise ValueError("Principal must be greater than zero")
    if annual_rate_bps < 0:
        raise ValueError("Interest rate cannot be negative")
    if term_months <= 0:
        raise ValueError("Term must be at least one month")
    if payment_frequency not in PAYMENT_FREQUENCIES:
        raise ValueError("Choose a valid payment frequency")
    if payment_frequency == "Accelerated biweekly":
        monthly = loan_payment_cents(
            principal_cents, annual_rate_bps, term_months, interest_convention, "Monthly",
        )
        return int((Decimal(monthly) / 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if payment_frequency == "Accelerated weekly":
        monthly = loan_payment_cents(
            principal_cents, annual_rate_bps, term_months, interest_convention, "Monthly",
        )
        return int((Decimal(monthly) / 4).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    periods_per_year = PAYMENTS_PER_YEAR[payment_frequency]
    periods = max(
        1,
        int(
            (Decimal(term_months) * periods_per_year / 12).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP,
            )
        ),
    )
    principal = Decimal(principal_cents)
    if annual_rate_bps == 0:
        return int((principal / periods).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    periodic_rate = periodic_interest_rate(
        annual_rate_bps, interest_convention, periods_per_year,
    )
    factor = (Decimal("1") + periodic_rate) ** periods
    payment = principal * periodic_rate * factor / (factor - Decimal("1"))
    return int(payment.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def payment_monthly_equivalent(payment_cents: int, payment_frequency: str) -> int:
    """Convert a contractual payment to an auditable average monthly cash amount."""
    try:
        periods = PAYMENTS_PER_YEAR[payment_frequency]
    except KeyError:
        raise ValueError("Choose a valid payment frequency") from None
    return int(
        (Decimal(payment_cents) * periods / 12).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        )
    )


def scheduled_balance_cents(
    principal_cents: int,
    annual_rate_bps: int,
    term_months: int,
    payments_made: int,
    interest_convention: str = "Monthly",
) -> int:
    """Return balance after scheduled payments, capped to the contractual term."""
    paid = min(max(int(payments_made), 0), term_months)
    if paid >= term_months:
        return 0
    payment = Decimal(loan_payment_cents(
        principal_cents, annual_rate_bps, term_months, interest_convention,
    ))
    principal = Decimal(principal_cents)
    if annual_rate_bps == 0:
        return max(0, int((principal - payment * paid).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    rate = monthly_interest_rate(annual_rate_bps, interest_convention)
    growth = (Decimal("1") + rate) ** paid
    balance = principal * growth - payment * (growth - Decimal("1")) / rate
    return max(0, int(balance.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def scheduled_payments_due(first_payment: date, as_of: date) -> int:
    """Count monthly due dates on or before ``as_of``, respecting short months."""
    if as_of < first_payment:
        return 0
    months = (as_of.year - first_payment.year) * 12 + as_of.month - first_payment.month
    due_day = min(first_payment.day, monthrange(as_of.year, as_of.month)[1])
    return months + (1 if as_of.day >= due_day else 0)


def exponential_average_cents(
    values: list[int] | tuple[int, ...], *, decay: Decimal = INCOME_FORECAST_DECAY,
) -> int | None:
    """Return a recency-weighted average of chronological cent values."""
    if not values:
        return None
    if not Decimal("0") < decay <= Decimal("1"):
        raise ValueError("Decay must be greater than zero and no more than one")
    latest = len(values) - 1
    weights = [decay ** (latest - index) for index in range(len(values))]
    weighted = sum(
        (Decimal(value) * weight for value, weight in zip(values, weights)), Decimal("0")
    )
    return int((weighted / sum(weights)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def balance_after_payments_cents(
    principal_cents: int, annual_rate_bps: int, payments: list[int] | tuple[int, ...],
    interest_convention: str = "Monthly",
) -> int:
    """Apply monthly payments with interest rounded to cents each period."""
    if principal_cents < 0 or annual_rate_bps < 0:
        raise ValueError("Principal and interest rate cannot be negative")
    balance = int(principal_cents)
    rate = monthly_interest_rate(annual_rate_bps, interest_convention)
    for payment in payments:
        if balance <= 0:
            return 0
        interest = int(
            (Decimal(balance) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        balance = max(0, balance + interest - max(int(payment), 0))
    return balance


def projected_payoff_months(
    balance_cents: int,
    annual_rate_bps: int,
    monthly_payment_cents: int,
    interest_convention: str = "Monthly",
    *,
    maximum_months: int = 1200,
) -> int | None:
    """Simulate a constant payment; return None when it cannot amortize the debt."""
    if balance_cents <= 0:
        return 0
    if monthly_payment_cents <= 0:
        return None
    balance = int(balance_cents)
    rate = monthly_interest_rate(annual_rate_bps, interest_convention)
    first_interest = int(
        (Decimal(balance) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if monthly_payment_cents <= first_interest:
        return None
    for month in range(1, maximum_months + 1):
        interest = int(
            (Decimal(balance) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        balance = balance + interest - monthly_payment_cents
        if balance <= 0:
            return month
    return None


def monthly_interest_rate(annual_rate_bps: int, convention: str = "Monthly") -> Decimal:
    """Convert a nominal annual rate to the contractual monthly periodic rate."""
    if annual_rate_bps < 0:
        raise ValueError("Interest rate cannot be negative")
    if convention not in INTEREST_CONVENTIONS:
        raise ValueError("Choose a valid interest convention")
    nominal = Decimal(annual_rate_bps) / Decimal("10000")
    if convention == "Monthly":
        return nominal / Decimal("12")
    # Canadian nominal mortgage rates are commonly compounded twice yearly,
    # not in advance. Convert the half-year factor to an equivalent month.
    half_year_factor = Decimal("1") + nominal / Decimal("2")
    return (half_year_factor.ln() / Decimal("6")).exp() - Decimal("1")


def periodic_interest_rate(
    annual_rate_bps: int, convention: str, periods_per_year: int,
) -> Decimal:
    """Convert the contractual nominal rate to another payment period."""
    if periods_per_year <= 0:
        raise ValueError("Payment frequency must have at least one period")
    monthly = monthly_interest_rate(annual_rate_bps, convention)
    if periods_per_year == 12:
        return monthly
    annual_factor = (Decimal("1") + monthly) ** 12
    return (annual_factor.ln() / Decimal(periods_per_year)).exp() - Decimal("1")
