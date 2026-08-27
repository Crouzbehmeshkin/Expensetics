from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .services import EXPENSE_KIND


@dataclass(frozen=True)
class TransactionInput:
    date: date
    amount: Decimal
    description: str
    category: str
    subcategory: str = ""
    purpose: str = ""
    expense_type: str = "Living"
    need_want: str = ""
    notes: str = ""
    source_key: str | None = None
    source_bank: str = ""
    source_vendor: str = ""
    source_vendor_key: str = ""
    transaction_kind: str = EXPENSE_KIND
    account_id: int | None = None


@dataclass(frozen=True)
class BankImportMetadata:
    filename: str
    bank: str
    first_transaction_date: date
    last_transaction_date: date
    source_row_count: int
    selected_row_count: int
    account_id: int | None = None


@dataclass(frozen=True)
class AccountInput:
    name: str
    account_type: str
    institution: str = ""
    last_four: str = ""
    is_active: bool = True


@dataclass(frozen=True)
class IncomeInput:
    date: date
    amount: Decimal
    description: str
    notes: str = ""


@dataclass(frozen=True)
class NetWorthInput:
    date: date
    assets: Decimal
    liabilities: Decimal = Decimal("0")
    notes: str = ""


@dataclass(frozen=True)
class LiabilityInput:
    name: str
    liability_type: str
    original_principal: Decimal
    annual_rate_percent: Decimal
    term_months: int
    start_date: date
    notes: str = ""
    payment_match_key: str = ""
    payment_match_label: str = ""
    rate_type: str = "Fixed"
    interest_convention: str = "Monthly"
    rate_term_months: int = 60
    current_balance: Decimal | None = None
    balance_as_of: date | None = None
    payment_frequency: str = "Monthly"
    payment_amount: Decimal | None = None


@dataclass(frozen=True)
class IncomeEstimateInput:
    month: str
    amount: Decimal
