from __future__ import annotations

import csv
import hashlib
import io
from itertools import islice
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .import_policy import MAX_CSV_BYTES, MAX_CSV_ROWS
from .models import TransactionInput
from .repository import Repository
from .services import (
    ANNUAL_EXPENSE_TYPE, most_used_subcategory, normalize_description,
    similar_subcategory,
)


@dataclass(frozen=True)
class ParsedBankRow:
    bank: str
    row_number: int
    transaction_date: date
    amount: Decimal
    vendor: str
    merchant_category: str
    reference: str
    source_key: str
    vendor_key: str
    eligible: bool = True
    exclusion_reason: str = ""


@dataclass
class ReviewRow:
    source: ParsedBankRow
    description: str
    category: str
    subcategory: str
    suggestion_source: str
    include: bool = True
    duplicate_reason: str = ""
    locked: bool = False
    annual_expense: bool = False
    needs_category_review: bool = False

    def transaction(self, account_id: int | None = None) -> TransactionInput:
        notes = f"Imported from {self.source.bank} CSV"
        if self.source.merchant_category:
            notes += f" · Bank category: {self.source.merchant_category}"
        return TransactionInput(
            date=self.source.transaction_date,
            amount=self.source.amount,
            description=self.description,
            category=self.category,
            subcategory=self.subcategory,
            expense_type=ANNUAL_EXPENSE_TYPE if self.annual_expense else "Living",
            notes=notes,
            source_key=account_source_key(self.source.source_key, account_id),
            source_bank=self.source.bank,
            source_vendor=self.source.vendor,
            source_vendor_key=self.source.vendor_key,
            account_id=account_id,
        )


@dataclass(frozen=True)
class ReviewBatch:
    bank: str
    filename: str
    rows: list[ReviewRow]


def decode_csv(data: bytes) -> str:
    if len(data) > MAX_CSV_BYTES:
        raise ValueError(
            f"The CSV exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MiB safety limit"
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    # Japanese banks publish Windows-compatible Shift-JIS (CP932) CSVs. Only
    # choose that decoder when it produces Japanese text; otherwise Western
    # CP1252 remains the conservative fallback for existing adapters.
    try:
        japanese = data.decode("cp932")
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", japanese):
            return japanese
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        pass
    raise ValueError("The CSV encoding is not supported")


def build_review_batch(
    csv_text: str, filename: str, repository: Repository, *, bank: str,
    account_id: int | None = None,
) -> ReviewBatch:
    parsed = parse_bank_csv(csv_text, bank)
    if not parsed:
        raise ValueError("The CSV does not contain any transaction rows")

    with repository.read_session() as reader:
        active_categories = reader.categories()
        learned_by_vendor = reader.vendor_preferences(
            parsed[0].bank, [source.vendor_key for source in parsed],
        )
        catalog = reader.description_catalog()
        derived_by_vendor: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        rows: list[ReviewRow] = []
        for source in parsed:
            learned = learned_by_vendor.get(source.vendor_key)
            if learned:
                description = learned["description"]
                category = learned["category"]
                subcategory = learned["subcategory"]
                confirmations = int(learned["uses"])
                suggestion_source = (
                    f"Learned from {confirmations} "
                    f"{'confirmation' if confirmations == 1 else 'confirmations'}"
                )
            else:
                cache_key = (source.vendor_key, source.merchant_category)
                cached = derived_by_vendor.get(cache_key)
                if cached is None:
                    cached = _historical_or_rule_suggestion(source, catalog, reader)
                    derived_by_vendor[cache_key] = cached
                description, category, subcategory, suggestion_source = cached
            needs_category_review = category not in active_categories
            if needs_category_review:
                unavailable = category
                category = "Other" if "Other" in active_categories else active_categories[0]
                subcategory = ""
                suggestion_source = (
                    f"Needs review · {unavailable} is archived"
                )

            rows.append(ReviewRow(
                source=source,
                description=description,
                category=category,
                subcategory=subcategory,
                suggestion_source=suggestion_source,
                include=source.eligible and not needs_category_review,
                needs_category_review=needs_category_review,
            ))
        batch = ReviewBatch(parsed[0].bank, filename, rows)
        apply_duplicate_status(batch, reader, account_id)
    return batch


def _historical_or_rule_suggestion(
    source: ParsedBankRow, catalog: list[dict], reader: Repository,
) -> tuple[str, str, str, str]:
    description, category, subcategory, suggestion_source = _rule_suggestion(source)
    queries = tuple(dict.fromkeys((
        description, _display_vendor(source.vendor), source.vendor_key,
    )))
    historical = None
    for query in queries:
        _, historical = reader.description_assistance(catalog, query, 0)
        if historical:
            break
    if historical:
        return (
            description, historical["category"], historical["subcategory"] or "",
            "Matched expense history",
        )
    if subcategory:
        return description, category, subcategory, suggestion_source

    similar = similar_subcategory(
        catalog, queries, category=None if category == "Other" else category,
    )
    if similar:
        return (
            description, similar["category"], similar["subcategory"],
            "Matched similar expense history · "
            f'{round(float(similar["similarity"]) * 100)}%',
        )
    subcategory = most_used_subcategory(catalog, category)
    if subcategory:
        suggestion_source = f"Most used subcategory in {category}"
    return description, category, subcategory, suggestion_source


def account_source_key(source_key: str, account_id: int | None) -> str:
    """Scope a bank row identity to its destination account."""
    account = str(account_id) if account_id is not None else "unassigned"
    return f"{source_key}:account:{account}"


def apply_duplicate_status(
    batch: ReviewBatch, repository: Repository, account_id: int | None,
) -> None:
    """Deterministically re-evaluate duplicate status after account changes."""
    eligible = [row for row in batch.rows if row.source.eligible]
    snapshot = repository.import_duplicate_snapshot(
        [account_source_key(row.source.source_key, account_id) for row in eligible],
        [row.source.source_key for row in eligible],
        [row.source.transaction_date for row in eligible],
        [int(row.source.amount * 100) for row in eligible],
        account_id,
    )
    seen_semantic: set[tuple[str, int, str]] = set()
    for row in batch.rows:
        source = row.source
        amount_cents = int(source.amount * 100)
        semantic_key = (source.transaction_date.isoformat(), amount_cents, source.vendor_key)
        row.duplicate_reason = ""
        row.locked = False
        row.include = source.eligible and not row.needs_category_review
        if not source.eligible:
            row.duplicate_reason = source.exclusion_reason
            row.locked = True
            row.include = False
        elif (
            account_source_key(source.source_key, account_id) in snapshot["sources"]
            or source.source_key in snapshot["sources"]
        ):
            row.duplicate_reason = "Already imported"
            row.locked = True
            row.include = False
        elif semantic_key in seen_semantic:
            row.duplicate_reason = "Possible duplicate in this file"
            row.include = False
        elif (
            semantic_key in snapshot["semantic_vendor"]
            or semantic_key in snapshot["semantic_description"]
            or (
                source.transaction_date.isoformat(), amount_cents,
                normalize_description(row.description),
            ) in snapshot["semantic_description"]
        ):
            row.duplicate_reason = "Possible duplicate in existing expenses"
            row.include = False
        seen_semantic.add(semantic_key)


def parse_bank_csv(csv_text: str, bank: str) -> list[ParsedBankRow]:
    raw_rows = _read_csv_rows(csv_text)
    if not raw_rows:
        raise ValueError("The CSV is empty")
    parser = BANK_PARSERS.get(bank)
    if parser is None:
        raise ValueError(f"Unsupported bank: {bank}")
    return parser(raw_rows)


def _read_csv_rows(csv_text: str) -> list[list[str]]:
    """Read comma, semicolon, or tab-delimited exports without guessing fields."""
    content = csv_text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(islice(csv.reader(io.StringIO(content), dialect), MAX_CSV_ROWS + 1))
    if len(rows) > MAX_CSV_ROWS:
        raise ValueError(f"The CSV exceeds the {MAX_CSV_ROWS:,}-row review limit")
    return rows


def _parse_bmo_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header_index = next((
        index for index, row in enumerate(raw_rows)
        if "Transaction Date" in row and "Transaction Amount" in row and "Description" in row
    ), None)
    if header_index is None:
        raise ValueError("This file does not match the selected BMO CSV format")
    return _parse_bmo(raw_rows[header_index:])


def _parse_rogers_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not {"Date", "Merchant Name", "Amount"}.issubset(header):
        raise ValueError("This file does not match the selected Rogers CSV format")
    return _parse_rogers(raw_rows)


def _parse_bmo(rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        amount = _amount(row.get("Transaction Amount") or "")
        transaction_date = _date(row.get("Transaction Date") or "", "%Y%m%d")
        item = (row.get("Item #") or str(row_number)).strip()
        card_last_four = re.sub(r"\D", "", row.get("Card #") or "")[-4:]
        vendor_key = canonical_vendor_key(vendor)
        reference = f"{item}:{card_last_four}"
        parsed.append(_parsed_row(
            "BMO", row_number, transaction_date, amount, vendor, "",
            reference, vendor_key,
        ))
    return parsed


def _parse_rogers(rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Merchant Name") or "").strip()
        if not vendor:
            continue
        amount = _amount(row.get("Amount") or "")
        transaction_date = _date(row.get("Date") or "", "%Y-%m-%d")
        category = (row.get("Merchant Category Description") or "").strip()
        reference = (row.get("Reference Number") or str(row_number)).strip()
        eligible = (row.get("Activity Status") or "APPROVED").upper() == "APPROVED"
        reason = "" if eligible else "Transaction is not approved"
        parsed.append(_parsed_row(
            "Rogers", row_number, transaction_date, amount, vendor, category,
            reference, canonical_vendor_key(vendor), eligible, reason,
        ))
    return parsed


def _parse_capital_one_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not {"Transaction Date", "Description"}.issubset(header) or not (
        {"Debit", "Credit"} & header
    ):
        raise ValueError("This file does not match the selected Capital One CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        transaction_date = _date_any(row.get("Transaction Date") or "")
        amount = _debit_credit_amount(row.get("Debit"), row.get("Credit"))
        category = (row.get("Category") or "").strip()
        card = re.sub(r"\D", "", row.get("Card No.") or row.get("Account Number") or "")[-4:]
        posted = (row.get("Posted Date") or "").strip()
        reference = f"{card}:{posted}:{row_number}"
        parsed.append(_parsed_row(
            "Capital One", row_number, transaction_date, amount, vendor,
            category, reference, canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_bank_of_america_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header_index = next((
        index for index, row in enumerate(raw_rows)
        if {"Date", "Description", "Amount"}.issubset(
            {value.strip() for value in row}
        )
    ), None)
    if header_index is None:
        raise ValueError(
            "This file does not match the selected Bank of America CSV format"
        )
    return _parse_signed_amount_rows(
        raw_rows[header_index:], bank="Bank of America",
        date_column="Date", description_column="Description",
        amount_column="Amount",
    )


def _parse_chase_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    required = {"Posting Date", "Description", "Amount"}
    if not required.issubset(header):
        raise ValueError("This file does not match the selected Chase CSV format")
    return _parse_signed_amount_rows(
        raw_rows, bank="Chase", date_column="Posting Date",
        description_column="Description", amount_column="Amount",
        category_column="Type", reference_column="Check or Slip #",
    )


def _parse_citi_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not {"Date", "Description"}.issubset(header) or not ({"Debit", "Credit"} & header):
        raise ValueError("This file does not match the selected Citi CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        parsed.append(_parsed_row(
            "Citi", row_number, _date_any(row.get("Date") or ""),
            _debit_credit_amount(row.get("Debit"), row.get("Credit")),
            vendor, "", str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_wells_fargo_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    rows = [row for row in raw_rows if any(value.strip() for value in row)]
    if not rows or not all(len(row) >= 5 and _is_date(row[0]) for row in rows):
        raise ValueError(
            "This file does not match the selected Wells Fargo CSV format"
        )
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(rows, start=1):
        vendor = row[4].strip()
        if not vendor:
            continue
        parsed.append(_parsed_row(
            "Wells Fargo", row_number, _date_any(row[0]), -_amount(row[1]),
            vendor, "", f"{row[2].strip()}:{row[3].strip()}:{row_number}",
            canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_american_express_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not {"Date", "Description", "Amount"}.issubset(header):
        raise ValueError(
            "This file does not match the selected American Express CSV format"
        )
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        account = re.sub(r"\D", "", row.get("Account #") or "")[-4:]
        parsed.append(_parsed_row(
            "American Express (US)", row_number,
            _date(row.get("Date") or "", "%m/%d/%Y"),
            _amount(row.get("Amount") or ""), vendor,
            (row.get("Category") or "").strip(),
            f"{account}:{row_number}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_apple_card_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    required = {"Transaction Date", "Type", "Amount (USD)"}
    if not required.issubset(header) or not ({"Description", "Merchant"} & header):
        raise ValueError("This file does not match the selected Apple Card CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    credit_types = {"credit", "payment", "refund"}
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Merchant") or row.get("Description") or "").strip()
        if not vendor:
            continue
        raw_amount = abs(_amount(row.get("Amount (USD)") or ""))
        transaction_type = (row.get("Type") or "").strip()
        amount = -raw_amount if transaction_type.casefold() in credit_types else raw_amount
        clearing_date = (row.get("Clearing Date") or "").strip()
        parsed.append(_parsed_row(
            "Apple Card", row_number,
            _date(row.get("Transaction Date") or "", "%m/%d/%Y"),
            amount, vendor, (row.get("Category") or "").strip(),
            f"{clearing_date}:{row_number}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_discover_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    required = {"Trans. Date", "Description", "Amount"}
    if not required.issubset(header):
        raise ValueError("This file does not match the selected Discover CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        parsed.append(_parsed_row(
            "Discover", row_number,
            _date(row.get("Trans. Date") or "", "%m/%d/%Y"),
            _amount(row.get("Amount") or ""), vendor,
            (row.get("Category") or "").strip(),
            f"{(row.get('Post Date') or '').strip()}:{row_number}",
            canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_us_bank_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    required = {"Date", "Amount"}
    if not required.issubset(header) or not ({"Name", "Memo"} & header):
        raise ValueError("This file does not match the selected U.S. Bank CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        name = (row.get("Name") or "").strip()
        memo = (row.get("Memo") or "").strip()
        vendor = " · ".join(value for value in (name, memo) if value)
        if not vendor:
            continue
        transaction_type = (row.get("Transaction") or "").strip()
        parsed.append(_parsed_row(
            "U.S. Bank", row_number,
            _date(row.get("Date") or "", "%m/%d/%Y"),
            -_amount(row.get("Amount") or ""), vendor, transaction_type,
            str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_desjardins_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    """Parse the official 13-column AccèsD/AccèsD Affaires positional CSV."""
    rows = [row for row in raw_rows if any(value.strip() for value in row)]
    if not rows or not all(
        len(row) >= 13 and _is_date_format(row[3], "%Y/%m/%d") for row in rows
    ):
        raise ValueError("This file does not match the selected Desjardins CSV format")

    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(rows, start=1):
        institution = row[0].strip()
        vendor = row[5].strip()
        if not vendor:
            continue
        is_visa = institution.casefold().startswith("visa")
        debit = row[10] if is_visa else row[7]
        credit = row[11] if is_visa else row[8]
        folio = re.sub(r"\D", "", row[1])[-4:]
        sequence = row[4].strip()
        parsed.append(_parsed_row(
            "Desjardins", row_number, _date(row[3], "%Y/%m/%d"),
            _debit_credit_amount(debit, credit), vendor, row[2].strip(),
            f"{folio}:{sequence}:{row_number}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_signed_amount_rows(
    rows: list[list[str]], *, bank: str, date_column: str,
    description_column: str, amount_column: str,
    category_column: str = "", reference_column: str = "",
) -> list[ParsedBankRow]:
    """Parse exports where withdrawals are negative and deposits are positive."""
    records = csv.DictReader(io.StringIO(_rows_to_csv(rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get(description_column) or "").strip()
        if not vendor:
            continue
        reference = (row.get(reference_column) or "").strip() if reference_column else ""
        parsed.append(_parsed_row(
            bank, row_number, _date_any(row.get(date_column) or ""),
            -_amount(row.get(amount_column) or ""), vendor,
            (row.get(category_column) or "").strip() if category_column else "",
            reference or str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_td_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = raw_rows[0] if raw_rows else []
    columns = {value.strip() for value in header}
    if not {"Date", "Description"}.issubset(columns) or not (
        {"Withdrawals", "Deposits", "Debit", "Credit"} & columns
    ):
        raise ValueError("This file does not match the selected TD CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        parsed.append(_parsed_row(
            "TD", row_number, _date_any(row.get("Date") or ""),
            _debit_credit_amount(
                row.get("Withdrawals") or row.get("Debit"),
                row.get("Deposits") or row.get("Credit"),
            ), vendor, (row.get("Transaction") or "").strip(),
            str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_cibc_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    if not raw_rows:
        raise ValueError("This file does not match the selected CIBC CSV format")
    header = {value.strip() for value in raw_rows[0]}
    if {"Transaction Date", "Description"}.issubset(header) and (
        {"Withdrawals", "Deposits"} & header
    ):
        return _parse_cibc_headered(raw_rows)
    if not all(len(row) >= 4 and _is_date(row[0]) for row in raw_rows if any(row)):
        raise ValueError("This file does not match the selected CIBC CSV format")

    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(raw_rows, start=1):
        if not any(row):
            continue
        vendor = row[1].strip()
        if not vendor:
            continue
        account = re.sub(r"\D", "", row[4] if len(row) > 4 else "")[-4:]
        parsed.append(_parsed_row(
            "CIBC", row_number, _date_any(row[0]),
            _debit_credit_amount(row[2], row[3]), vendor, "",
            f"{account}:{row_number}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_cibc_headered(rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        parsed.append(_parsed_row(
            "CIBC", row_number, _date_any(row.get("Transaction Date") or ""),
            _debit_credit_amount(row.get("Withdrawals"), row.get("Deposits")),
            vendor, "", str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_scotiabank_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if {"Date", "Description", "Amount"}.issubset(header):
        return _parse_scotiabank_signed(raw_rows)
    if {"Date", "Description"}.issubset(header) and (
        {"Withdrawal", "Deposit", "Withdrawals", "Deposits"} & header
    ):
        return _parse_scotiabank_debit_credit(raw_rows)
    raise ValueError("This file does not match the selected Scotiabank CSV format")


def _parse_scotiabank_signed(rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        description = (row.get("Description") or "").strip()
        subdescription = (row.get("Sub-description") or "").strip()
        vendor = " · ".join(value for value in (description, subdescription) if value)
        if not vendor:
            continue
        signed = _amount(row.get("Amount") or "")
        parsed.append(_parsed_row(
            "Scotiabank", row_number, _date_any(row.get("Date") or ""),
            -signed, vendor, (row.get("Type of Transaction") or "").strip(),
            str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_scotiabank_debit_credit(rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        parsed.append(_parsed_row(
            "Scotiabank", row_number, _date_any(row.get("Date") or ""),
            _debit_credit_amount(
                row.get("Withdrawal") or row.get("Withdrawals"),
                row.get("Deposit") or row.get("Deposits"),
            ), vendor, "", str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_rbc_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    required = {"Transaction Date", "Description 1"}
    if not required.issubset(header) or not ({"CAD$", "USD$"} & header):
        raise ValueError("This file does not match the selected RBC CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = " · ".join(value for value in (
            (row.get("Description 1") or "").strip(),
            (row.get("Description 2") or "").strip(),
        ) if value)
        if not vendor:
            continue
        cad_value = (row.get("CAD$") or "").strip()
        usd_value = (row.get("USD$") or "").strip()
        eligible = bool(cad_value)
        reason = "" if eligible else "USD transaction requires manual currency conversion"
        signed = _amount(cad_value or usd_value)
        account = re.sub(r"\D", "", row.get("Account #") or "")[-4:]
        cheque = (row.get("Cheque Number") or "").strip()
        parsed.append(_parsed_row(
            "RBC", row_number, _date_any(row.get("Transaction Date") or ""),
            -signed, vendor, "", f"{account}:{cheque}:{row_number}",
            canonical_vendor_key(vendor), eligible, reason,
        ))
    return parsed


def _parse_monzo_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    required = {"Date", "Amount"}
    if not required.issubset(header) or not ({"Name", "Description"} & header):
        raise ValueError("This file does not match the selected Monzo CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Name") or row.get("Description") or "").strip()
        if not vendor:
            continue
        signed = _amount_localized(row.get("Amount") or "")
        category = (row.get("Category") or row.get("Type") or "").strip()
        reference = (row.get("Transaction ID") or str(row_number)).strip()
        parsed.append(_parsed_row(
            "Monzo", row_number, _date_european(row.get("Date") or ""),
            -signed, vendor, category, reference, canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_n26_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = [value.strip() for value in (raw_rows[0] if raw_rows else [])]
    columns = set(header)
    date_column = "Booking Date" if "Booking Date" in columns else "Date"
    vendor_column = "Partner Name" if "Partner Name" in columns else "Payee"
    amount_column = next((value for value in header if value.startswith("Amount (")), "")
    if not amount_column or date_column not in columns or vendor_column not in columns:
        raise ValueError("This file does not match the selected N26 CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get(vendor_column) or row.get("Payment Reference") or "").strip()
        if not vendor:
            continue
        signed = _amount_localized(row.get(amount_column) or "")
        category = (row.get("Category") or row.get("Type") or row.get("Transaction type") or "").strip()
        iban = (row.get("Partner Iban") or row.get("Account number") or "").strip()
        reference = ":".join(filter(None, (
            iban, (row.get("Payment Reference") or "").strip(), str(row_number),
        )))
        parsed.append(_parsed_row(
            "N26", row_number, _date_european(row.get(date_column) or ""),
            -signed, vendor, category, reference, canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_revolut_business_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    statement_required = {"Description", "Amount", "State"}
    expenses_required = {
        "Transaction completed (UTC)", "Transaction status",
        "Amount (payment currency)",
    }
    if statement_required.issubset(header) and ({"Completed Date", "Started Date"} & header):
        return _parse_revolut_business_statement(raw_rows)
    if expenses_required.issubset(header) and (
        {"Transaction description", "Expense description"} & header
    ):
        return _parse_revolut_business_expenses(raw_rows)
    raise ValueError("This file does not match a supported Revolut Business CSV format")


def _parse_revolut_business_statement(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Description") or "").strip()
        if not vendor:
            continue
        completed = (row.get("Completed Date") or row.get("Started Date") or "").strip()
        state = (row.get("State") or "").strip().upper()
        eligible = state == "COMPLETED"
        reason = "" if eligible else f"Transaction state is {state.lower() or 'unknown'}"
        signed = _amount_localized(row.get("Amount") or "")
        reference = ":".join((completed, (row.get("Type") or "").strip(), str(row_number)))
        parsed.append(_parsed_row(
            "Revolut Business", row_number, _date_european(completed),
            -signed, vendor, (row.get("Type") or "").strip(), reference,
            canonical_vendor_key(vendor), eligible, reason,
        ))
    return parsed


def _parse_revolut_business_expenses(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (
            row.get("Transaction description")
            or row.get("Expense description")
            or ""
        ).strip()
        if not vendor:
            continue
        completed = (row.get("Transaction completed (UTC)") or "").strip()
        state = (row.get("Transaction status") or "").strip().upper()
        eligible = state == "COMPLETED"
        reason = "" if eligible else f"Transaction status is {state.lower() or 'unknown'}"
        signed = _amount_localized(row.get("Amount (payment currency)") or "")
        reference = ":".join(filter(None, (
            (row.get("Transaction ID") or "").strip(),
            (row.get("Expense split #") or "").strip(),
        )))
        category = (
            row.get("Expense category name")
            or row.get("Transaction type")
            or ""
        ).strip()
        parsed.append(_parsed_row(
            "Revolut Business", row_number, _date_european(completed),
            -signed, vendor, category, reference or str(row_number),
            canonical_vendor_key(vendor), eligible, reason,
        ))
    return parsed


def _parse_starling_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = [value.strip() for value in (raw_rows[0] if raw_rows else [])]
    columns = set(header)
    vendor_column = "Counter Party" if "Counter Party" in columns else "Counterparty"
    amount_column = next((value for value in header if value.startswith("Amount (")), "")
    if not amount_column or "Date" not in columns or vendor_column not in columns:
        raise ValueError("This file does not match the selected Starling CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        counterparty = (row.get(vendor_column) or "").strip()
        reference_text = (row.get("Reference") or "").strip()
        vendor = counterparty or reference_text
        if not vendor:
            continue
        signed = _amount_localized(row.get(amount_column) or "")
        reference = f"{reference_text}:{row_number}"
        parsed.append(_parsed_row(
            "Starling", row_number, _date_european(row.get("Date") or ""),
            -signed, vendor, (row.get("Spending Category") or row.get("Type") or "").strip(),
            reference, canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_wise_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not {"Date", "Amount"}.issubset(header) or not (
        {"Merchant", "Payee Name", "Description", "Payer Name"} & header
    ):
        raise ValueError("This file does not match the selected Wise CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = next((
            (row.get(column) or "").strip()
            for column in ("Merchant", "Payee Name", "Description", "Payer Name")
            if (row.get(column) or "").strip()
        ), "")
        if not vendor:
            continue
        signed = _amount_localized(row.get("Amount") or "")
        reference = next((
            (row.get(column) or "").strip()
            for column in ("TransferWise ID", "Reference Number", "Payment Reference")
            if (row.get(column) or "").strip()
        ), str(row_number))
        parsed.append(_parsed_row(
            "Wise", row_number, _date_european(row.get("Date") or ""),
            -signed, vendor, "", reference, canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_rabobank_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if {"Creditcard Nummer", "Datum", "Bedrag", "Omschrijving"}.issubset(header):
        return _parse_rabobank_credit_card(raw_rows)
    if {"Datum", "Bedrag"}.issubset(header) and (
        {"Naam tegenpartij", "Naam uiteindelijke partij", "Omschrijving-1"} & header
    ):
        return _parse_rabobank_transactions(raw_rows, language="nl")
    if {"Date", "Amount"}.issubset(header) and (
        {"Name Counterpty", "Name Ultimate Pty", "Description-1"} & header
    ):
        return _parse_rabobank_transactions(raw_rows, language="en")
    raise ValueError("This file does not match a supported Rabobank CSV format")


def _parse_rabobank_transactions(
    raw_rows: list[list[str]], *, language: str,
) -> list[ParsedBankRow]:
    columns = {
        "date": "Datum" if language == "nl" else "Date",
        "amount": "Bedrag" if language == "nl" else "Amount",
        "currency": "Munt" if language == "nl" else "Ccy",
        "sequence": "Volgnr" if language == "nl" else "Seq No",
        "counterparty": "Naam tegenpartij" if language == "nl" else "Name Counterpty",
        "ultimate": "Naam uiteindelijke partij" if language == "nl" else "Name Ultimate Pty",
        "description": "Omschrijving" if language == "nl" else "Description",
        "code": "Code",
    }
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        descriptions = [
            (row.get(f'{columns["description"]}-{index}') or "").strip()
            for index in range(1, 4)
        ]
        vendor = next((
            (row.get(column) or "").strip()
            for column in (columns["counterparty"], columns["ultimate"])
            if (row.get(column) or "").strip()
        ), next((value for value in descriptions if value), ""))
        if not vendor:
            continue
        signed = _amount_localized(row.get(columns["amount"]) or "")
        account = (row.get("IBAN/BBAN") or "").strip()
        currency = (row.get(columns["currency"]) or "").strip()
        sequence = (row.get(columns["sequence"]) or str(row_number)).strip()
        parsed.append(_parsed_row(
            "Rabobank", row_number, _date(row.get(columns["date"]) or "", "%Y-%m-%d"),
            -signed, vendor, (row.get(columns["code"]) or "").strip(),
            f"{account}:{currency}:{sequence}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_rabobank_credit_card(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("Omschrijving") or "").strip()
        if not vendor:
            continue
        signed = _amount_localized(row.get("Bedrag") or "")
        account = (row.get("Tegenrekening IBAN") or "").strip()
        card = re.sub(r"\D", "", row.get("Creditcard Nummer") or "")[-4:]
        transaction_reference = (row.get("Transactiereferentie") or str(row_number)).strip()
        parsed.append(_parsed_row(
            "Rabobank", row_number, _date(row.get("Datum") or "", "%Y-%m-%d"),
            -signed, vendor, (row.get("Productnaam") or "").strip(),
            f"{account}:{card}:{transaction_reference}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_bunq_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not {"Date", "Amount"}.issubset(header) or not (
        {"Name", "Counterparty", "Description"} & header
    ):
        raise ValueError("This file does not match the selected bunq CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = next((
            (row.get(column) or "").strip()
            for column in ("Name", "Counterparty", "Description")
            if (row.get(column) or "").strip()
        ), "")
        if not vendor:
            continue
        signed = _amount_localized(row.get("Amount") or "")
        account = (row.get("Account") or "").strip()
        parsed.append(_parsed_row(
            "bunq", row_number, _date_european(row.get("Date") or ""),
            -signed, vendor, "", f"{account}:{row_number}", canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_mufg_bizstation_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    """Parse MUFG BizSTATION's documented positional all-transactions CSV."""
    rows = [row for row in raw_rows if any(value.strip() for value in row)]
    if not rows or not rows[0] or rows[0][0].strip() != "1":
        raise ValueError("This file does not match the selected MUFG BizSTATION CSV format")

    header = rows[0]
    # The account number improves source keys but is not required to import.
    # Transaction records only need fields 1-6 from MUFG's published format.
    account = header[6].strip() if len(header) > 6 else ""
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(rows[1:], start=2):
        record_type = row[0].strip() if row else ""
        if record_type in {"8", "9"}:
            continue
        if record_type != "2" or len(row) < 6:
            raise ValueError("This file does not match the selected MUFG BizSTATION CSV format")
        vendor = row[3].strip() or row[2].strip()
        if not vendor:
            continue
        amount = _japanese_debit_credit_amount(row[4], row[5])
        parsed.append(_parsed_row(
            "MUFG BizSTATION", row_number, _date(row[1], "%Y.%m.%d"),
            amount, vendor, row[2].strip(), account, canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_mizuho_business_web_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    """Parse the named subset of Mizuho Business WEB's published CSV."""
    required = {"勘定日", "摘要"}
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    if not required.issubset(header) or not ({"出金（円）", "入金（円）"} & header):
        raise ValueError("This file does not match the selected Mizuho Business WEB CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get("摘要") or "").strip() or (row.get("取引区分") or "").strip()
        if not vendor:
            continue
        amount = _japanese_debit_credit_amount(row.get("出金（円）"), row.get("入金（円）"))
        account = (row.get("照会口座") or "").strip()
        reference = (row.get("番号") or "").strip() or account
        parsed.append(_parsed_row(
            "Mizuho Business WEB", row_number,
            _date(row.get("勘定日") or "", "%Y年%m月%d日"), amount, vendor,
            (row.get("取引区分") or "").strip(), f"{account}:{reference}",
            canonical_vendor_key(vendor),
        ))
    return parsed


def _parse_smbc_direct_export(raw_rows: list[list[str]]) -> list[ParsedBankRow]:
    """Parse the fields SMBC Direct documents as mirrored in its consumer CSV."""
    header = {value.strip() for value in (raw_rows[0] if raw_rows else [])}
    description_column = next((
        name for name in ("お取り扱い内容", "お取引内容") if name in header
    ), None)
    if not description_column or "日付" not in header or not (
        {"お引出し", "お預入れ"} & header
    ):
        raise ValueError("This file does not match the selected SMBC Direct CSV format")
    records = csv.DictReader(io.StringIO(_rows_to_csv(raw_rows)))
    parsed: list[ParsedBankRow] = []
    for row_number, row in enumerate(records, start=2):
        vendor = (row.get(description_column) or "").strip()
        if not vendor:
            continue
        amount = _japanese_debit_credit_amount(row.get("お引出し"), row.get("お預入れ"))
        parsed.append(_parsed_row(
            "SMBC Direct", row_number, _date_japanese_consumer(row.get("日付") or ""),
            amount, vendor, "", str(row_number), canonical_vendor_key(vendor),
        ))
    return parsed


BANK_PARSERS = {
    "American Express (US)": _parse_american_express_export,
    "Apple Card": _parse_apple_card_export,
    "BMO": _parse_bmo_export,
    "Bank of America": _parse_bank_of_america_export,
    "Capital One": _parse_capital_one_export,
    "Chase": _parse_chase_export,
    "CIBC": _parse_cibc_export,
    "Citi": _parse_citi_export,
    "Desjardins": _parse_desjardins_export,
    "Discover": _parse_discover_export,
    "RBC": _parse_rbc_export,
    "Rogers": _parse_rogers_export,
    "Scotiabank": _parse_scotiabank_export,
    "TD": _parse_td_export,
    "U.S. Bank": _parse_us_bank_export,
    "Wells Fargo": _parse_wells_fargo_export,
    "Monzo": _parse_monzo_export,
    "N26": _parse_n26_export,
    "Rabobank": _parse_rabobank_export,
    "Revolut Business": _parse_revolut_business_export,
    "Starling": _parse_starling_export,
    "Wise": _parse_wise_export,
    "bunq": _parse_bunq_export,
    "MUFG BizSTATION": _parse_mufg_bizstation_export,
    "Mizuho Business WEB": _parse_mizuho_business_web_export,
    "SMBC Direct": _parse_smbc_direct_export,
}
SUPPORTED_BANKS = tuple(BANK_PARSERS)

NORTH_AMERICAN_BANKS = (
    "American Express (US)", "Apple Card", "BMO", "Bank of America",
    "Capital One", "Chase", "CIBC", "Citi", "Desjardins", "Discover",
    "RBC", "Rogers", "Scotiabank", "TD", "U.S. Bank", "Wells Fargo",
)
EUROPEAN_BANKS = (
    "Monzo", "N26", "Rabobank", "Revolut Business", "Starling", "Wise", "bunq",
)
ASIAN_BANKS = ("MUFG BizSTATION", "Mizuho Business WEB", "SMBC Direct")
BANK_GROUPS = (
    ("Canada & United States", NORTH_AMERICAN_BANKS),
    ("Europe", EUROPEAN_BANKS),
    ("Asia", ASIAN_BANKS),
)


def _parsed_row(
    bank: str, row_number: int, transaction_date: date, amount: Decimal,
    vendor: str, merchant_category: str, reference: str,
    vendor_key: str, eligible: bool = True, exclusion_reason: str = "",
) -> ParsedBankRow:
    if amount <= 0:
        eligible = False
        exclusion_reason = "Credit or payment—not an expense"
    fingerprint = "|".join((
        bank.lower(), reference, transaction_date.isoformat(),
        f"{amount:.2f}", vendor_key,
    ))
    source_key = f"bank-csv:{bank.lower()}:{hashlib.sha256(fingerprint.encode()).hexdigest()[:32]}"
    return ParsedBankRow(
        bank, row_number, transaction_date, amount, vendor,
        merchant_category, reference, source_key, vendor_key, eligible, exclusion_reason,
    )


def canonical_vendor_key(vendor: str) -> str:
    normalized = normalize_description(re.sub(r"[^\w&']+", " ", vendor))
    aliases = (
        (("fido",), "fido"), (("shell",), "shell"), (("presto",), "presto"),
        (("jesse's nf", "no frills"), "no frills"), (("loblaw",), "loblaw"),
        (("super unique",), "super unique"), (("farm boy",), "farm boy"),
        (("hmart",), "hmart"), (("cobs",), "cobs"), (("costco",), "costco"),
        (("spotify",), "spotify"), (("metergy",), "metergy"),
        (("aviva",), "aviva"), (("rally enterprises",), "rally enterprises"),
    )
    for needles, key in aliases:
        if any(needle in normalized for needle in needles):
            return key
    normalized = re.sub(r"\b[a-z]*\d[a-z\d]*\b", "", normalized)
    return normalize_description(normalized)


def _rule_suggestion(source: ParsedBankRow) -> tuple[str, str, str, str]:
    known = {
        "fido": ("Phone bill", "Bills & Utilities", "Phone"),
        "shell": ("Gas", "Transportation", "Gas"),
        "presto": ("Presto", "Transportation", "Transit"),
        "no frills": ("No Frills", "Groceries", "Groceries"),
        "loblaw": ("Loblaw", "Groceries", "Groceries"),
        "super unique": ("Unique", "Groceries", "Groceries"),
        "farm boy": ("Farm Boy", "Groceries", "Groceries"),
        "hmart": ("H Mart", "Groceries", "Groceries"),
        "cobs": ("COBS", "Groceries", "Bakery"),
        "costco": ("Costco", "Groceries", "Warehouse"),
        "spotify": ("Spotify", "Bills & Utilities", "Subscriptions"),
        "metergy": ("Utilities", "Bills & Utilities", "Utilities"),
        "aviva": ("Car insurance", "Transportation", "Insurance"),
        "rally enterprises": ("Internet", "Bills & Utilities", "Internet"),
    }
    if source.vendor_key in known:
        return (*known[source.vendor_key], "Recognized vendor")

    display = _display_vendor(source.vendor)
    category_text = normalize_description(source.merchant_category)
    category_rules = (
        (("telecommunication",), "Bills & Utilities", "Phone"),
        (("internet access", "computer network"), "Bills & Utilities", "Internet"),
        (("utilities",), "Bills & Utilities", "Utilities"),
        (("insurance",), "Transportation", "Insurance"),
        (("service station", "fuel", "gasoline"), "Transportation", "Gas"),
        (("supermarket", "food store", "wholesale club", "bakeries"), "Groceries", "Groceries"),
        (("restaurant", "fast food", "eating places"), "Dining", "Dining"),
        (("optician", "eyeglass"), "Health", "Vision"),
        (("digital goods", "music"), "Bills & Utilities", "Subscriptions"),
    )
    for needles, category, subcategory in category_rules:
        if any(needle in category_text for needle in needles):
            return display, category, subcategory, "Suggested from bank category"
    return display, "Other", "", "Needs review"


def _display_vendor(vendor: str) -> str:
    cleaned = re.sub(r"^SQ \*", "", vendor, flags=re.IGNORECASE)
    cleaned = re.sub(r"\*{3,}\d+", "", cleaned)
    cleaned = re.sub(r"\b[A-Z]\d{4,}\b$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" *-")
    return cleaned.title() if cleaned.isupper() else cleaned


def _amount(value: str) -> Decimal:
    cleaned = value.strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"Invalid transaction amount: {value}") from None
    if not amount.is_finite():
        raise ValueError(f"Invalid transaction amount: {value}")
    return amount


def _amount_localized(value: str) -> Decimal:
    """Parse a signed amount from either decimal-point or decimal-comma CSVs.

    The final separator is treated as the decimal mark when both comma and dot
    are present. A lone comma is a decimal mark, matching European exports.
    Currency symbols, ISO currency codes, spaces, and accounting parentheses
    are accepted; ambiguous non-numeric text is rejected.
    """
    raw = value.strip().replace("\u00a0", "").replace(" ", "").replace("'", "")
    negative_parentheses = raw.startswith("(") and raw.endswith(")")
    cleaned = re.sub(r"[^0-9,\.\-+]", "", raw.strip("()"))
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    if negative_parentheses:
        cleaned = f"-{cleaned.lstrip('+')}"
    try:
        amount = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"Invalid transaction amount: {value}") from None
    if not amount.is_finite():
        raise ValueError(f"Invalid transaction amount: {value}")
    return amount


def _debit_credit_amount(debit: str | None, credit: str | None) -> Decimal:
    debit_value = _optional_amount(debit)
    credit_value = _optional_amount(credit)
    if debit_value is not None and debit_value != 0:
        return abs(debit_value)
    if credit_value is not None and credit_value != 0:
        return -abs(credit_value)
    raise ValueError("Transaction row has neither a debit nor a credit amount")


def _optional_amount(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    return _amount(value)


def _japanese_optional_amount(value: str | None) -> Decimal | None:
    if value is None or value.strip() in {"", "-", "－"}:
        return None
    return _amount(value)


def _japanese_debit_credit_amount(debit: str | None, credit: str | None) -> Decimal:
    debit_value = _japanese_optional_amount(debit)
    credit_value = _japanese_optional_amount(credit)
    if debit_value is not None and debit_value != 0:
        return abs(debit_value)
    if credit_value is not None and credit_value != 0:
        return -abs(credit_value)
    raise ValueError("Transaction row has neither a withdrawal nor a deposit amount")


def _date(value: str, format_string: str) -> date:
    try:
        return datetime.strptime(value.strip(), format_string).date()
    except ValueError:
        raise ValueError(f"Invalid transaction date: {value}") from None


def _date_any(value: str) -> date:
    for format_string in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip(), format_string).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid transaction date: {value}")


def _date_european(value: str) -> date:
    """Parse only unambiguous ISO or day-first dates used by EU/UK exports."""
    cleaned = value.strip()
    formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d/%m/%y",
        "%d.%m.%Y", "%d.%m.%y", "%d-%m-%Y", "%d-%m-%y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S",
    )
    for format_string in formats:
        try:
            return datetime.strptime(cleaned, format_string).date()
        except ValueError:
            continue
    # Revolut may append fractional seconds or a timezone to an ISO timestamp.
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", cleaned):
        try:
            return date.fromisoformat(cleaned[:10])
        except ValueError:
            pass
    raise ValueError(f"Invalid European transaction date: {value}")


def _date_japanese_consumer(value: str) -> date:
    for format_string in ("%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(value.strip(), format_string).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid Japanese transaction date: {value}")


def _is_date(value: str) -> bool:
    try:
        _date_any(value)
    except ValueError:
        return False
    return True


def _is_date_format(value: str, format_string: str) -> bool:
    try:
        _date(value, format_string)
    except ValueError:
        return False
    return True


def _rows_to_csv(rows: list[list[str]]) -> str:
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    return output.getvalue()
