from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings, strategies as st

from finance_app.db import initialize
from finance_app.formatting import money
from finance_app.models import BankImportMetadata, TransactionInput
from finance_app.repository import Repository
from finance_app.services import (
    INTEREST_CONVENTIONS,
    EXPENSE_KIND,
    SETTLEMENT_KIND,
    allocate_cents,
    balance_after_payments_cents,
    exponential_average_cents,
    ngram_similarity,
    parse_transaction_amount,
    shifted_month,
    weighted_income_forecast,
)


PROPERTY_TMP_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"


@given(
    total_cents=st.integers(min_value=-(10**12), max_value=10**12),
    periods=st.integers(min_value=1, max_value=120),
)
@settings(max_examples=200, deadline=None)
def test_cent_allocation_preserves_every_cent(total_cents: int, periods: int) -> None:
    allocation = allocate_cents(total_cents, periods)

    assert len(allocation) == periods
    assert sum(allocation) == total_cents
    assert max(map(abs, allocation)) - min(map(abs, allocation)) <= 1
    assert list(map(abs, allocation)) == sorted(map(abs, allocation), reverse=True)
    assert allocate_cents(-total_cents, periods) == tuple(-value for value in allocation)


@given(cents=st.integers(min_value=1, max_value=10**12))
@settings(max_examples=200)
def test_formatted_money_round_trips_to_signed_storage_cents(cents: int) -> None:
    assert parse_transaction_amount(money(cents), EXPENSE_KIND) == cents
    assert parse_transaction_amount(money(-cents), SETTLEMENT_KIND) == -cents


@given(
    year=st.integers(min_value=1900, max_value=2100),
    month=st.integers(min_value=1, max_value=12),
    offset=st.integers(min_value=-1200, max_value=1200),
)
@settings(max_examples=200)
def test_month_shifts_are_reversible(year: int, month: int, offset: int) -> None:
    original = f"{year:04d}-{month:02d}"
    assert shifted_month(shifted_month(original, offset), -offset) == original


@given(
    left=st.text(max_size=50),
    right=st.text(max_size=50),
    size=st.integers(min_value=1, max_value=8),
)
@settings(max_examples=200)
def test_ngram_similarity_is_symmetric_and_bounded(
    left: str, right: str, size: int,
) -> None:
    forward = ngram_similarity(left, right, size)
    reverse = ngram_similarity(right, left, size)

    assert forward == reverse
    assert 0.0 <= forward <= 1.0
    if any(character.isalnum() for character in left):
        assert ngram_similarity(left, left, size) == 1.0


@given(
    principal=st.integers(min_value=0, max_value=10**10),
    annual_rate_bps=st.integers(min_value=0, max_value=2500),
    payment_pairs=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**8),
            st.integers(min_value=0, max_value=10**8),
        ),
        max_size=60,
    ),
    convention=st.sampled_from(INTEREST_CONVENTIONS),
)
@settings(max_examples=150, deadline=None)
def test_larger_payment_streams_never_increase_the_remaining_balance(
    principal: int,
    annual_rate_bps: int,
    payment_pairs: list[tuple[int, int]],
    convention: str,
) -> None:
    base = [payment for payment, _ in payment_pairs]
    larger = [payment + extra for payment, extra in payment_pairs]

    base_balance = balance_after_payments_cents(
        principal, annual_rate_bps, base, convention,
    )
    larger_balance = balance_after_payments_cents(
        principal, annual_rate_bps, larger, convention,
    )

    assert 0 <= larger_balance <= base_balance


@given(
    amount_cents=st.integers(min_value=1, max_value=10**10),
    observations=st.integers(min_value=1, max_value=60),
)
@settings(max_examples=120, deadline=None)
def test_constant_income_history_produces_the_same_forecast(
    amount_cents: int, observations: int,
) -> None:
    months = [shifted_month("2010-01", offset) for offset in range(observations)]
    forecast = weighted_income_forecast(
        [(month, amount_cents) for month in months],
        target_month=shifted_month(months[-1], 1),
    )

    assert forecast.amount_cents == amount_cents
    assert forecast.observations == observations
    assert forecast.source_months == tuple(months)


@given(
    values=st.lists(
        st.integers(min_value=0, max_value=10**10),
        min_size=1,
        max_size=80,
    ),
)
@settings(max_examples=150, deadline=None)
def test_exponential_average_stays_within_observed_values(values: list[int]) -> None:
    average = exponential_average_cents(values)

    assert average is not None
    assert min(values) <= average <= max(values)


@given(
    amounts=st.lists(
        st.integers(min_value=1, max_value=10**7),
        min_size=1,
        max_size=8,
    ),
    unselected_rows=st.integers(min_value=0, max_value=30),
)
@settings(max_examples=30, deadline=None)
def test_reviewed_import_history_matches_the_atomic_transaction_batch(
    amounts: list[int], unselected_rows: int,
) -> None:
    PROPERTY_TMP_ROOT.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="property-import-", dir=PROPERTY_TMP_ROOT) as directory:
        database = Path(directory) / "finance.db"
        initialize(database)
        repository = Repository(database)
        items = [
            TransactionInput(
                date=date(2026, 8, index + 1),
                amount=Decimal(cents) / Decimal(100),
                description=f"Generated merchant {index}",
                category="Other",
                source_key=f"generated-source-{index}",
                source_bank="BMO",
                source_vendor=f"Generated merchant {index}",
                source_vendor_key=f"generated merchant {index}",
            )
            for index, cents in enumerate(amounts)
        ]
        metadata = BankImportMetadata(
            filename="generated-statement.csv",
            bank="BMO",
            first_transaction_date=items[0].date,
            last_transaction_date=items[-1].date,
            source_row_count=len(items) + unselected_rows,
            selected_row_count=len(items),
        )
        invalid_metadata = BankImportMetadata(
            filename=metadata.filename,
            bank=metadata.bank,
            first_transaction_date=metadata.first_transaction_date,
            last_transaction_date=metadata.last_transaction_date,
            source_row_count=metadata.source_row_count,
            selected_row_count=len(items) + 1,
        )

        with pytest.raises(ValueError, match="Selected import row count"):
            repository.add_bank_import(items, invalid_metadata)
        assert repository.list("2026-08") == []
        assert repository.recent_bank_imports() == []

        assert repository.add_bank_import(items, metadata) == len(items)
        assert sum(row["amount_cents"] for row in repository.list("2026-08")) == sum(amounts)
        history = repository.recent_bank_imports()
        assert len(history) == 1
        assert history[0]["source_row_count"] == len(items) + unselected_rows
        assert history[0]["selected_row_count"] == len(items)
        assert history[0]["imported_count"] == len(items)
        assert not list(Path(directory).glob("*.csv"))
