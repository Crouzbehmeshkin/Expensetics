from __future__ import annotations

from nicegui import ui

from .bank_import import (
    BANK_GROUPS, SUPPORTED_BANKS, ReviewBatch, ReviewRow, apply_duplicate_status,
    build_review_batch, decode_csv,
)
from .formatting import account_label, date_label, money
from .i18n import translate, translated_options
from .import_policy import MAX_CSV_BYTES, configure_memory_only_uploads
from .models import BankImportMetadata
from .repository import Repository
from .services import ranked_subcategory_options, subcategory_selection
from .session_security import AuthorizationExpired


configure_memory_only_uploads()


BANK_MARKS = {
    "American Express (US)": ("AX", "american-express"),
    "Apple Card": ("A", "apple-card"),
    "BMO": ("BMO", "bmo"),
    "Bank of America": ("BA", "bank-of-america"),
    "Capital One": ("C1", "capital-one"),
    "Chase": ("◇", "chase"),
    "CIBC": ("C", "cibc"),
    "Citi": ("citi", "citi"),
    "Desjardins": ("D", "desjardins"),
    "Discover": ("D", "discover"),
    "Monzo": ("M", "monzo"),
    "N26": ("N26", "n26"),
    "Rabobank": ("R", "rabobank"),
    "RBC": ("RBC", "rbc"),
    "Rogers": ("R", "rogers"),
    "Revolut Business": ("R", "revolut"),
    "Scotiabank": ("S", "scotiabank"),
    "TD": ("TD", "td"),
    "Starling": ("S", "starling"),
    "U.S. Bank": ("US", "us-bank"),
    "Wells Fargo": ("WF", "wells-fargo"),
    "Wise": ("W", "wise"),
    "bunq": ("bq", "bunq"),
    "MUFG BizSTATION": ("MU", "mufg"),
    "Mizuho Business WEB": ("MZ", "mizuho"),
    "SMBC Direct": ("SM", "smbc"),
}


class BankImportDialog:
    def __init__(self, repository: Repository, on_imported) -> None:
        self.repository = repository
        self.on_imported = on_imported
        self.batch: ReviewBatch | None = None
        self.selected_bank: str | None = None
        self.selected_account_id: int | None = None
        self.error_message = ""
        self.account_records: list[dict] = []
        self.category_options: list[str] = []
        self.subcategory_options: dict[str, list[str]] = {}
        self.preferred_subcategories: dict[str, str] = {}
        self.dialog = ui.dialog()
        with self.dialog, ui.card().classes("bank-import-card"):
            self.content()

    def open(self) -> None:
        with self.repository.read_session() as reader:
            self.account_records = reader.accounts()
            category_library = reader.category_library()
            self.category_options = [
                category["name"] for category in category_library if category["is_active"]
            ]
            (
                self.subcategory_options,
                self.preferred_subcategories,
            ) = ranked_subcategory_options(category_library)
        self.batch = None
        self.selected_bank = None
        self.selected_account_id = None
        self.error_message = ""
        self.content.refresh()
        self.dialog.open()

    @ui.refreshable
    def content(self) -> None:
        with ui.row().classes("editor-head items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(translate("Import bank transactions")).classes("section-title")
                ui.label(
                    "Review every purchase before it enters your books."
                ).classes("muted text-xs")
            ui.button(icon="close", on_click=self.dialog.close).props(
                f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
            )

        if self.batch is None:
            with ui.column().classes("bank-upload-body items-center w-full"):
                if self.selected_bank is None:
                    ui.icon("account_balance").classes("bank-upload-icon")
                    ui.label("Which bank exported the transactions?").classes("section-title")
                    ui.label(
                        "Choose the bank first so Expensetics can apply its exact CSV format."
                    ).classes("muted text-sm text-center")
                    with ui.element("div").classes("bank-selector-scroll"):
                        for region, banks in BANK_GROUPS:
                            ui.label(translate(region)).classes("bank-region-label")
                            with ui.element("div").classes("bank-selector-grid"):
                                for bank in banks:
                                    mark, style = BANK_MARKS[bank]
                                    with ui.button(
                                        on_click=lambda selected=bank: self._select_bank(selected),
                                    ).props(
                                        f"flat no-caps aria-label='Import {bank} CSV'"
                                    ).classes("bank-option"):
                                        ui.label(mark).classes(f"bank-mark bank-mark-{style}")
                                        ui.label(bank).classes("bank-option-name")
                else:
                    with ui.column().classes("bank-upload-stage w-full"):
                        with ui.row().classes(
                            "bank-upload-stage-head items-center justify-between w-full"
                        ):
                            with ui.column().classes("gap-1"):
                                ui.label(
                                    f"Choose your {self.selected_bank} CSV export"
                                ).classes("section-title")
                                ui.label(
                                    "The file stays local. Credits and repeat imports are excluded automatically."
                                ).classes("muted text-xs")
                            with ui.row().classes("items-center gap-2"):
                                ui.label(self.selected_bank).classes("insight-badge")
                                ui.button(
                                    translate("Change bank"), on_click=self._change_bank
                                ).props("flat dense no-caps")

                        with ui.element("div").classes("bank-upload-options"):
                            with ui.column().classes("bank-file-choice"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("upload_file").classes("bank-file-icon")
                                    ui.label(translate("Select CSV")).classes(
                                        "bank-account-title"
                                    )
                                    ui.label(translate("Required")).classes(
                                        "required-indicator"
                                    )
                                ui.upload(
                                    on_upload=self._uploaded,
                                    on_rejected=lambda: self._show_error(
                                        f"Choose one CSV file no larger than "
                                        f"{MAX_CSV_BYTES // (1024 * 1024)} MiB."
                                    ),
                                    auto_upload=True,
                                    max_file_size=MAX_CSV_BYTES,
                                    max_total_size=MAX_CSV_BYTES,
                                    max_files=1,
                                    label=translate("Drop a CSV here or choose a file"),
                                ).props(
                                    "accept=.csv flat bordered color=transparent "
                                    "text-color=primary"
                                ).classes("bank-uploader")
                                ui.label(
                                    translate("Drag and drop supported · CSV only")
                                ).classes("bank-upload-hint")
                            self._account_picker()
                if self.error_message:
                    ui.label(self.error_message).classes("field-error text-center")
            return

        flagged = [row for row in self.batch.rows if row.duplicate_reason]
        categories = self.category_options
        with ui.column().classes("bank-review-body gap-3 w-full"):
            with ui.row().classes("items-center justify-between w-full no-wrap bank-review-summary"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(self.batch.bank).classes("insight-badge")
                    ui.label(self.batch.filename).classes("font-medium text-sm")
                    ui.label(f"{len(self.batch.rows)} bank rows").classes("muted text-xs")
                with ui.row().classes("items-center gap-3"):
                    self.selected_label = ui.label("").classes("text-sm font-medium")
                    self._update_selected_label()
                    if flagged:
                        ui.label(f"{len(flagged)} flagged").classes("duplicate-badge")

            with ui.row().classes("items-center justify-between w-full"):
                ui.label(
                    "Edit the details or mark an annual cost for 12-month allocation."
                ).classes("muted text-xs")
                with ui.row().classes("gap-1"):
                    ui.button(translate("Include clean rows"), on_click=self._include_clean).props(
                        "flat dense no-caps"
                    )
                    ui.button(translate("Clear"), on_click=self._clear_selection).props(
                        "flat dense no-caps color=grey-7"
                    )

            self._account_picker(compact=True)

            with ui.element("div").classes("bank-review-scroll"):
                with ui.element("div").classes("bank-review-grid bank-review-header"):
                    for heading in (
                        "Import", "Date", "Bank vendor", "Amount", "Description",
                        "Category", "Subcategory", "Annual", "Review",
                    ):
                        ui.label(translate(heading))
                for row in self.batch.rows:
                    self._review_row(row, categories)

            if self.error_message:
                ui.label(self.error_message).classes("field-error")

        with ui.row().classes("editor-actions items-center justify-between w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.button(translate("Change bank"), on_click=self._change_bank).props(
                    "flat no-caps color=grey-7"
                )
                ui.button(translate("Choose another file"), on_click=self._choose_another_file).props(
                    "flat no-caps color=grey-7"
                )
            with ui.row().classes("items-center gap-2"):
                ui.button(translate("Cancel"), on_click=self.dialog.close).props("flat no-caps color=grey-7")
                ui.button(translate("Import selected"), icon="check", on_click=self._commit).props(
                    "unelevated no-caps"
                ).classes("primary")

    def _review_row(self, row: ReviewRow, categories: list[str]) -> None:
        row_class = "bank-review-grid bank-review-row"
        if row.duplicate_reason:
            row_class += " flagged"
        if row.locked:
            row_class += " locked"
        with ui.element("div").classes(row_class):
            checkbox = ui.checkbox(
                value=row.include,
                on_change=lambda event, item=row: self._set_include(item, event.value),
            ).props("dense")
            if row.locked:
                checkbox.disable()
            ui.label(date_label(row.source.transaction_date.isoformat(), include_year=True)).classes(
                "bank-cell-date"
            )
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(row.source.vendor).classes("bank-vendor ellipsis")
                if row.source.merchant_category:
                    ui.label(row.source.merchant_category).classes(
                        "muted bank-merchant-category ellipsis"
                    )
            ui.label(f"${row.source.amount:,.2f}").classes("amount")
            ui.input(
                value=row.description,
                on_change=lambda event, item=row: setattr(item, "description", event.value or ""),
            ).props("outlined dense").classes("bank-description")
            suggestion_label = None
            def update_category(event, item=row) -> None:
                item.category = event.value or ""
                options, item.subcategory = subcategory_selection(
                    self.subcategory_options, item.category, item.subcategory,
                )
                if item.subcategory:
                    item.suggestion_source = (
                        f"Available under the selected {item.category} category"
                    )
                else:
                    item.subcategory = self.preferred_subcategories.get(item.category, "")
                    item.suggestion_source = (
                        f"Most used subcategory in {item.category}"
                        if item.subcategory
                        else "Category changed · optional subcategory not suggested"
                    )
                subcategory_select.options = options
                subcategory_select.value = item.subcategory or None
                subcategory_select.update()
                if suggestion_label is not None:
                    suggestion_label.text = item.suggestion_source
                if item.needs_category_review and not item.locked:
                    item.needs_category_review = False
                    item.include = True
                    checkbox.value = True
                    checkbox.update()
                    self._update_selected_label()

            ui.select(
                translated_options(tuple(categories)), value=row.category,
                on_change=update_category,
            ).props(
                "outlined dense options-dense "
                "popup-content-class=theme-select-menu"
            ).classes("bank-category")
            subcategory_options, row.subcategory = subcategory_selection(
                self.subcategory_options, row.category, row.subcategory,
                preserve_unknown=True,
            )
            subcategory_select = ui.select(
                subcategory_options, value=row.subcategory or None,
                label=translate("Optional"), with_input=True,
                new_value_mode="add-unique", clearable=True,
                on_change=lambda event, item=row: setattr(item, "subcategory", event.value or ""),
            ).props(
                "outlined dense options-dense popup-content-class=theme-select-menu"
            ).classes("bank-subcategory")
            ui.checkbox(
                value=row.annual_expense,
                on_change=lambda event, item=row: setattr(
                    item, "annual_expense", bool(event.value),
                ),
            ).props(
                f"dense aria-label='{translate('Annual expense')}'"
            ).classes("bank-annual-expense")
            with ui.column().classes("gap-0"):
                if row.duplicate_reason:
                    ui.label(row.duplicate_reason).classes("duplicate-text")
                    if not row.locked:
                        ui.label("Can be included manually").classes("muted text-xs")
                else:
                    suggestion_label = ui.label(row.suggestion_source).classes(
                        "suggestion-source"
                    )

    async def _uploaded(self, event) -> None:
        self.error_message = ""
        try:
            if self.selected_bank is None:
                raise ValueError("Choose a bank before selecting a CSV")
            if event.file.size() > MAX_CSV_BYTES:
                raise ValueError(
                    f"The CSV exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MiB safety limit"
                )
            data = await event.file.read()
            self.batch = build_review_batch(
                decode_csv(data), event.file.name, self.repository,
                bank=self.selected_bank, account_id=self.selected_account_id,
            )
        except ValueError as error:
            self.batch = None
            self.error_message = str(error)
        except AuthorizationExpired:
            raise
        except Exception:
            self.batch = None
            self.error_message = "The CSV could not be read. No data was changed."
        self.content.refresh()

    def _select_bank(self, bank: str) -> None:
        self.selected_bank = bank if bank in SUPPORTED_BANKS else None
        matches = (
            self.repository.matching_accounts(self.account_records, bank)
            if self.selected_bank else []
        )
        self.selected_account_id = matches[0]["id"] if len(matches) == 1 else None
        self.error_message = ""
        self.content.refresh()

    def _change_bank(self) -> None:
        self.selected_bank = None
        self.selected_account_id = None
        self.batch = None
        self.error_message = ""
        self.content.refresh()

    def _choose_another_file(self) -> None:
        self.batch = None
        self.error_message = ""
        self.content.refresh()

    def _account_picker(self, *, compact: bool = False) -> None:
        if self.selected_bank is None:
            return
        all_accounts = self.account_records
        matches = self.repository.matching_accounts(all_accounts, self.selected_bank)
        candidates = matches or all_accounts
        valid_ids = {account["id"] for account in candidates}
        if self.selected_account_id not in valid_ids:
            self.selected_account_id = matches[0]["id"] if len(matches) == 1 else None
        with ui.column().classes(
            "bank-account-picker" + (" compact" if compact else "")
        ):
            with ui.row().classes("bank-account-heading items-start no-wrap"):
                ui.icon("account_balance_wallet").classes("bank-account-icon")
                with ui.column().classes("gap-0 min-w-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(translate("Account for this import")).classes(
                            "bank-account-title"
                        )
                        ui.label(translate("Optional")).classes("optional-indicator")
                    ui.label(
                        translate(
                            "Assigning an account makes these transactions easier to identify later."
                        )
                    ).classes("bank-account-intro")

            if not all_accounts:
                with ui.row().classes("bank-account-empty items-center no-wrap"):
                    with ui.column().classes("gap-0 min-w-0"):
                        ui.label(translate("No account selected")).classes(
                            "bank-account-state"
                        )
                        ui.label(
                            translate("You can continue unassigned or add an account now.")
                        ).classes("muted text-xs")
                    ui.button(
                        translate("Add account"),
                        icon="add_card",
                        on_click=lambda: ui.navigate.to("/accounts", new_tab=True),
                    ).props("outline no-caps").classes("bank-account-action")
                return

            options = {
                0: translate("Not assigned"),
                **{account["id"]: account_label(account) for account in candidates},
            }
            with ui.row().classes("bank-account-controls items-center no-wrap"):
                ui.select(
                    options,
                    label=translate("Account"),
                    value=self.selected_account_id or 0,
                    on_change=self._set_account,
                ).props(
                    "outlined dense options-dense popup-content-class=theme-select-menu"
                ).classes("bank-account-select")
                ui.button(
                    translate("Manage accounts"),
                    icon="open_in_new",
                    on_click=lambda: ui.navigate.to("/accounts", new_tab=True),
                ).props("flat dense no-caps").classes("bank-account-manage")
            if len(matches) == 1:
                ui.label(
                    translate(
                        "Automatically selected: the only {bank} account.",
                        bank=self.selected_bank,
                    )
                ).classes("bank-account-helper suggestion-source")
            elif len(matches) > 1:
                ui.label(
                    translate(
                        "Showing {count} accounts registered with {bank}.",
                        count=len(matches), bank=self.selected_bank,
                    )
                ).classes("bank-account-helper muted text-xs")
            elif candidates:
                ui.label(
                    translate("No institution match; showing all active accounts.")
                ).classes("bank-account-helper muted text-xs")

    def _set_account(self, event) -> None:
        value = int(event.value or 0)
        self.selected_account_id = value or None
        if self.batch:
            with self.repository.read_session() as reader:
                apply_duplicate_status(self.batch, reader, self.selected_account_id)
            self.content.refresh()

    def _show_error(self, message: str) -> None:
        self.error_message = message
        self.content.refresh()

    def _set_include(self, row: ReviewRow, value: bool) -> None:
        if not row.locked:
            row.include = bool(value)
        self._update_selected_label()

    def _include_clean(self) -> None:
        for row in self.batch.rows if self.batch else []:
            row.include = (
                not row.locked
                and not row.duplicate_reason
                and not row.needs_category_review
            )
        self.content.refresh()

    def _clear_selection(self) -> None:
        for row in self.batch.rows if self.batch else []:
            row.include = False
        self.content.refresh()

    def _update_selected_label(self) -> None:
        if not self.batch or not hasattr(self, "selected_label"):
            return
        selected = [row for row in self.batch.rows if row.include and not row.locked]
        total_cents = sum(int(row.source.amount * 100) for row in selected)
        self.selected_label.text = f"{len(selected)} selected · {money(total_cents)}"

    def _commit(self) -> None:
        if not self.batch:
            return
        selected = [row for row in self.batch.rows if row.include and not row.locked]
        if not selected:
            self.error_message = "Select at least one purchase to import."
            self.content.refresh()
            return
        incomplete = [
            row for row in selected if not row.description.strip() or not row.category
        ]
        if incomplete:
            self.error_message = (
                f"Complete the description and category for {len(incomplete)} selected row(s)."
            )
            self.content.refresh()
            return
        try:
            source_dates = [row.source.transaction_date for row in self.batch.rows]
            metadata = BankImportMetadata(
                filename=self.batch.filename,
                bank=self.batch.bank,
                account_id=self.selected_account_id,
                first_transaction_date=min(source_dates),
                last_transaction_date=max(source_dates),
                source_row_count=len(self.batch.rows),
                selected_row_count=len(selected),
            )
            imported = self.repository.add_bank_import(
                [row.transaction(self.selected_account_id) for row in selected],
                metadata,
            )
        except ValueError as error:
            self.error_message = f"Import could not be completed: {error}"
            self.content.refresh()
            return
        except AuthorizationExpired:
            raise
        except Exception:
            self.error_message = "The import failed. No transactions were added."
            self.content.refresh()
            return
        self.dialog.close()
        ui.notify(
            f"Imported {imported} transaction{'s' if imported != 1 else ''}",
            color="positive", position="bottom", timeout=2200,
        )
        self.on_imported()
