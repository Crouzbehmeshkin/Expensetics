from __future__ import annotations

from .i18n import date_name, month_name


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{remainder:02d}"


def display_name(value: str) -> str:
    cleaned = value.strip()
    return cleaned.upper() if len(cleaned) <= 3 else cleaned.title()


def account_label(account: dict) -> str:
    label = account["name"]
    if account.get("last_four"):
        label += f' · •••• {account["last_four"]}'
    return label


def month_label(month: str, short: bool = False) -> str:
    return month_name(month, short=short)


def date_label(value: str, *, include_year: bool = False) -> str:
    return date_name(value, include_year=include_year)
