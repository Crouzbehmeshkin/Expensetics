from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .services import parse_amount, parse_nonnegative_amount


def positive_amount(value, field_name: str = "Amount") -> Decimal:
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is required.")
    try:
        parsed = Decimal(str(value))
        parse_amount(parsed)
        return parsed
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {field_name.lower()} greater than zero.") from None


def nonnegative_amount(
    value, field_name: str, *, required: bool = True,
) -> Decimal:
    if value is None or not str(value).strip():
        if required:
            raise ValueError(f"{field_name} is required.")
        return Decimal("0")
    try:
        parsed = Decimal(str(value))
        parse_nonnegative_amount(parsed, field_name)
        return parsed
    except (InvalidOperation, ValueError):
        raise ValueError(f"Enter a valid {field_name.lower()} of zero or more.") from None


def required_date(value, field_name: str = "Date") -> date:
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is required.")
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"Choose a valid {field_name.lower()}.") from None


def required_text(value, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required.")
    return cleaned
