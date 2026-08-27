from __future__ import annotations

from datetime import date, timedelta

from nicegui import ui

from .bank_import import SUPPORTED_BANKS
from .components import chip_button
from .formatting import account_label
from .i18n import translate, translated_options
from .models import (
    AccountInput, IncomeEstimateInput, IncomeInput, LiabilityInput, NetWorthInput,
    TransactionInput,
)
from .repository import Repository
from .session_security import AuthorizationExpired
from .services import (
    ACCOUNT_TYPES, ANNUAL_EXPENSE_TYPE, EXPENSE_KIND, INTEREST_CONVENTIONS, LIABILITY_TYPES,
    MORTGAGE_RATE_TYPES, PAYMENT_FREQUENCIES, SETTLEMENT_KIND,
    ranked_subcategory_options, subcategory_selection,
)
from .state import ENTRY_DEFAULTS
from .validation import (
    nonnegative_amount, positive_amount, required_date, required_text,
)

def form_label(text: str, *, required: bool = False, hint: str | None = None) -> None:
    with ui.row().classes("items-center gap-2 no-wrap"):
        ui.label(translate(text)).classes("field-label mb-0")
        if required:
            ui.label(translate("Required")).classes("required-indicator")
        elif hint:
            ui.label(translate(hint)).classes("optional-indicator")


def field_error() -> ui.label:
    label = ui.label("").classes("field-error")
    label.set_visibility(False)
    return label


def set_field_error(label: ui.label, message: str = "") -> None:
    label.text = message
    label.set_visibility(bool(message))


def validated_value(parser, error_label: ui.label, *args, **kwargs):
    try:
        return parser(*args, **kwargs)
    except ValueError as error:
        set_field_error(error_label, str(error))
        return None


class AccountEditor:
    def __init__(self, repository: Repository, on_saved):
        self.repository = repository
        self.on_saved = on_saved
        self.identifier: int | None = None
        self.is_active = True
        with ui.dialog() as self.dialog, ui.card().classes("small-editor-card account-editor-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                self.heading = ui.label(translate("Add account")).classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body gap-3 w-full"):
                with ui.column().classes("gap-0 w-full"):
                    form_label("Account name", required=True)
                    self.name = ui.input(
                        placeholder=translate("e.g. RBC Visa, Main chequing"),
                    ).props("outlined dense").classes("w-full")
                    self.name_error = field_error()
                    self.name.on_value_change(lambda _: set_field_error(self.name_error))
                with ui.column().classes("gap-0 w-full"):
                    form_label("Account type", required=True)
                    self.account_type = ui.select(
                        translated_options(ACCOUNT_TYPES), value="Chequing",
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("w-full")
                    self.type_error = field_error()
                    self.account_type.on_value_change(lambda _: set_field_error(self.type_error))
                with ui.column().classes("gap-0 w-full"):
                    form_label("Institution", hint="Optional")
                    self.institution = ui.input(
                        placeholder=translate("Bank or card issuer"),
                        autocomplete=list(SUPPORTED_BANKS),
                    ).props("outlined dense autocomplete=off").classes("w-full")
                    ui.label(
                        translate(
                            "Using the importer’s bank name enables automatic account narrowing."
                        )
                    ).classes("muted text-xs mt-1")
                with ui.column().classes("gap-0 w-full"):
                    form_label("Last four digits", hint="Optional")
                    self.last_four = ui.input(placeholder="1234").props(
                        "outlined dense maxlength=4 inputmode=numeric"
                    ).classes("w-full")
                    self.error = field_error()
                    self.last_four.on_value_change(lambda _: set_field_error(self.error))
            with ui.row().classes("editor-actions justify-end w-full"):
                ui.button(translate("Cancel"), on_click=self.dialog.close).props(
                    "flat no-caps color=grey-7"
                )
                ui.button(translate("Save account"), on_click=self.save).props(
                    "unelevated no-caps"
                ).classes("primary")

    def open(self, record: dict | None = None) -> None:
        self.identifier = record["id"] if record else None
        self.is_active = bool(record.get("is_active", True)) if record else True
        self.heading.text = translate("Edit account" if record else "Add account")
        self.name.value = record["name"] if record else ""
        self.account_type.value = record["account_type"] if record else "Chequing"
        self.institution.value = record["institution"] if record else ""
        self.last_four.value = record["last_four"] if record else ""
        set_field_error(self.name_error)
        set_field_error(self.type_error)
        set_field_error(self.error)
        self.dialog.open()
        ui.timer(0.1, lambda: self.name.run_method("focus"), once=True)

    def save(self) -> None:
        name = validated_value(required_text, self.name_error, self.name.value, "Account name")
        if not self.account_type.value:
            set_field_error(self.type_error, "Choose an account type.")
        if name is None or not self.account_type.value:
            return
        try:
            self.identifier = self.repository.save_account(AccountInput(
                name=name,
                account_type=self.account_type.value,
                institution=self.institution.value or "",
                last_four=self.last_four.value or "",
                is_active=self.is_active,
            ), self.identifier)
        except ValueError as error:
            set_field_error(self.error, str(error))
            return
        except AuthorizationExpired:
            raise
        except Exception:
            set_field_error(self.error, "The account could not be saved. Your data was not changed.")
            return
        self.dialog.close()
        ui.notify("Account saved", color="positive", position="bottom")
        self.on_saved()


class ExpenseEditor:
    def __init__(self, repository: Repository, on_saved):
        self.repository = repository
        self.on_saved = on_saved
        self.identifier: int | None = None
        self.selected_category = ""
        self.category_was_predicted = False
        self.suggestion_rows: list[dict] = []
        self.category_options: list[str] = []
        self.subcategory_options: dict[str, list[str]] = {}
        self.description_catalog: list[dict] = []
        self.dialog = ui.dialog()
        with self.dialog, ui.card().classes("editor-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                self.heading = ui.label(translate("Add expense")).classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body expense-editor-body gap-3 w-full"):
                with ui.row().classes("w-full gap-3 items-start entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Amount", required=True)
                        self.amount = ui.input(placeholder="0.00").props(
                            "outlined dense inputmode=decimal prefix='$'"
                        ).classes("amount-input w-full")
                        self.amount_error = field_error()
                    with ui.column().classes("gap-0 date-field"):
                        form_label("Date", required=True)
                        with ui.row().classes("date-stepper items-center no-wrap w-full"):
                            ui.button(
                                icon="chevron_left", on_click=lambda: self._shift_date(-1),
                            ).props(
                                f"flat round dense tabindex=-1 aria-label='{translate('Previous day')}' title='{translate('Previous day')}'"
                            )
                            self.date = ui.input().props(
                                "outlined dense type=date tabindex=-1"
                            ).classes("date-stepper-input")
                            ui.button(
                                icon="chevron_right", on_click=lambda: self._shift_date(1),
                            ).props(
                                f"flat round dense tabindex=-1 aria-label='{translate('Next day')}' title='{translate('Next day')}'"
                            )
                        self.date_error = field_error()
                with ui.row().classes("items-center gap-3 transaction-kind-row"):
                    self.transaction_kind = ui.toggle(
                        translated_options((EXPENSE_KIND, SETTLEMENT_KIND)),
                        value=EXPENSE_KIND,
                        on_change=self._kind_changed,
                    ).props("no-caps dense unelevated tabindex=-1").classes(
                        "transaction-kind-toggle"
                    )
                    self.kind_help = ui.label(
                        "Subtracts from the selected category and outgoing cash."
                    ).classes("settlement-help")
                    self.kind_help.set_visibility(False)
                with ui.column().classes("gap-0 w-full relative-position"):
                    form_label("Description", required=True)
                    self.description = ui.input(placeholder=translate("Merchant or expense")).props(
                        "outlined dense autocomplete=off"
                    ).classes("w-full")
                    self.description_error = field_error()
                    self.description.on_value_change(self._description_input)
                    self.suggestions_view()
                with ui.column().classes("gap-2 w-full"):
                    with ui.row().classes("items-center gap-2"):
                        form_label("Category", required=True)
                        self.prediction_label = ui.label("").classes("text-caption muted")
                    self.category_view()
                    self.category_error = field_error()
                self.more_open = False
                ui.button(translate("More details"), icon="expand_more", on_click=self._toggle_more).props(
                    "flat dense no-caps color=grey-7"
                ).classes("self-start")
                with ui.column().classes("w-full gap-3") as self.more_panel:
                    self.more_panel.set_visibility(False)
                    with ui.column().classes("gap-0 w-full") as self.account_panel:
                        form_label("Account", hint="Optional")
                        self.account = ui.select(
                            {0: translate("Not assigned")}, value=0,
                        ).props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("w-full")
                    with ui.row().classes("optional-grid gap-3 w-full no-wrap"):
                        self.subcategory = ui.select(
                            [], label=translate("Subcategory"), with_input=True,
                            new_value_mode="add-unique", clearable=True,
                        ).props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("col")
                        self.purpose = ui.input(label=translate("Purpose")).props(
                            "outlined dense"
                        ).classes("col")
                    self.annual_allocation = ui.checkbox(
                        translate("Annual allocation"), value=False,
                    ).classes("annual-expense-row")
                    ui.label(
                        "Spreads this expense or settlement across exactly 12 months."
                    ).classes("muted text-xs annual-expense-help")
                    self.notes = ui.textarea(label=translate("Notes")).props("outlined dense autogrow").classes("w-full")
                self.error = field_error()
            with ui.row().classes("editor-actions items-center justify-between w-full"):
                self.shortcut_hint = ui.html(
                    '<span class="muted text-caption"><span class="shortcut">Esc</span> close &nbsp; '
                    '<span class="shortcut">Enter</span> save & next</span>'
                )
                with ui.row().classes("items-center gap-2"):
                    self.save_close_button = ui.button(
                        translate("Save & close"), on_click=lambda: self.save(continue_entry=False),
                    ).props("flat no-caps").classes("secondary-action")
                    self.save_button = ui.button(
                        translate("Save & next"), on_click=self.save,
                    ).props("unelevated no-caps").classes("primary")
        self.amount.on(
            "keydown.tab",
            js_handler=(
                "(event) => { event.preventDefault(); "
                f"document.getElementById('{self.description.html_id}')?.focus(); }}"
            ),
        )
        self.description.on(
            "keydown.tab", self._accept_top,
            js_handler=(
                "(event) => { event.preventDefault(); emit(); "
                f"document.getElementById('{self.save_button.html_id}')?.focus(); }}"
            ),
        )
        self.amount.on("keydown.enter", self.save)
        self.description.on("keydown.enter", self._enter_description)
        self.date.on("keydown.enter", self.save)
        self.amount.on_value_change(lambda _: set_field_error(self.amount_error))
        self.date.on_value_change(lambda _: set_field_error(self.date_error))

    @ui.refreshable
    def suggestions_view(self) -> None:
        if not self.suggestion_rows:
            return
        with ui.column().classes("suggestions gap-0"):
            for item in self.suggestion_rows:
                with ui.row().classes("suggestion items-center justify-between w-full").on(
                    "click", lambda _, choice=item: self.accept_suggestion(choice)
                ):
                    ui.label(item["description"])
                    detail = item["category"]
                    if item.get("subcategory"):
                        detail += f" · {item['subcategory']}"
                    ui.label(detail).classes("muted text-caption")

    @ui.refreshable
    def category_view(self) -> None:
        with ui.row().classes("chip-grid"):
            categories = list(self.category_options)
            if self.selected_category and self.selected_category not in categories:
                categories.append(self.selected_category)
            for category in categories:
                chip_button(
                    category, category == self.selected_category,
                    lambda _, value=category: self.select_category(value),
                )

    def open(self, record: dict | None = None) -> None:
        with self.repository.read_session() as reader:
            category_library = reader.category_library()
            self.category_options = [
                category["name"] for category in category_library if category["is_active"]
            ]
            self.subcategory_options, _ = ranked_subcategory_options(category_library)
            accounts = reader.accounts(include_inactive=True)
            self.description_catalog = reader.description_catalog()
        self.identifier = record["id"] if record else None
        transaction_kind = record.get("transaction_kind", EXPENSE_KIND) if record else EXPENSE_KIND
        self.heading.text = (
            translate("Edit settlement") if record and transaction_kind == SETTLEMENT_KIND
            else translate("Edit expense") if record else translate("Add expense")
        )
        self.amount.value = f'{abs(record["amount_cents"]) / 100:.2f}' if record else ""
        self.transaction_kind.value = transaction_kind
        self.kind_help.set_visibility(transaction_kind == SETTLEMENT_KIND)
        self.description.value = record["description"] if record else ""
        self.date.value = record["date"] if record else ENTRY_DEFAULTS["date"]
        self.selected_category = record["category"] if record else ""
        self.category_was_predicted = False
        self.subcategory.value = record.get("subcategory", "") if record else ""
        self._refresh_subcategory_options()
        self.annual_allocation.value = bool(
            record and record["expense_type"] == ANNUAL_EXPENSE_TYPE
        )
        self.existing_need_want = record.get("need_want", "") if record else ""
        self.purpose.value = record["purpose"] if record else ""
        self.notes.value = record["notes"] if record else ""
        if record and record.get("account_id"):
            visible_accounts = [
                account for account in accounts
                if account["is_active"] or account["id"] == record["account_id"]
            ]
        else:
            visible_accounts = [account for account in accounts if account["is_active"]]
        self.account.options = {
            0: translate("Not assigned"),
            **{account["id"]: account_label(account) for account in visible_accounts},
        }
        selected_account = (
            record.get("account_id") if record else ENTRY_DEFAULTS.get("account_id", 0)
        ) or 0
        if selected_account not in self.account.options:
            selected_account = 0
        self.account.value = selected_account
        self.account.update()
        self.account_panel.set_visibility(bool(visible_accounts))
        self._clear_errors()
        self.prediction_label.text = ""
        self.save_close_button.set_visibility(record is None)
        self.save_button.text = translate("Save changes") if record else translate("Save & next")
        self.shortcut_hint.content = (
            '<span class="muted text-caption"><span class="shortcut">Esc</span> close &nbsp; '
            f'<span class="shortcut">Enter</span> {"save" if record else "save & next"}</span>'
        )
        self.suggestion_rows = []
        self.suggestions_view.refresh()
        self.category_view.refresh()
        self.dialog.open()
        ui.timer(0.12, lambda: self.amount.run_method("focus"), once=True)

    def _description_input(self, event) -> None:
        set_field_error(self.description_error)
        value = event.value or ""
        self.suggestion_rows, predicted = self.repository.description_assistance(
            self.description_catalog, value,
        )
        self.suggestions_view.refresh()
        if predicted:
            self.select_category(
                predicted["category"], predicted=True,
                subcategory=predicted["subcategory"] or "",
            )
        elif self.category_was_predicted:
            self.selected_category = ""
            self.category_was_predicted = False
            self.subcategory.value = ""
            self.prediction_label.text = ""
            self.category_view.refresh()
            self._refresh_subcategory_options()

    def _accept_top(self, _) -> None:
        if self.suggestion_rows:
            self.accept_suggestion(self.suggestion_rows[0])

    def _enter_description(self, _) -> None:
        if self.suggestion_rows:
            self.accept_suggestion(self.suggestion_rows[0])
        else:
            self.save()

    def accept_suggestion(self, item: dict) -> None:
        self.description.set_value(item["description"])
        ui.timer(0.05, lambda: self.description.run_method("updateValue"), once=True)
        self.suggestion_rows = []
        self.suggestions_view.refresh()
        self.select_category(
            item["category"], predicted=True,
            subcategory=item.get("subcategory") or "",
        )

    def select_category(
        self, value: str, predicted: bool = False, subcategory: str | None = None,
    ) -> None:
        category_changed = value != self.selected_category
        self.selected_category = value
        if subcategory is not None:
            self.subcategory.value = subcategory
        self.category_was_predicted = predicted
        set_field_error(self.category_error)
        self.prediction_label.text = translate("Suggested from history") if predicted else ""
        self.category_view.refresh()
        self._refresh_subcategory_options(
            preserve_current=subcategory is not None or not category_changed,
        )

    def _refresh_subcategory_options(self, *, preserve_current: bool = True) -> None:
        options, selected = subcategory_selection(
            self.subcategory_options, self.selected_category, self.subcategory.value,
            preserve_unknown=preserve_current,
        )
        self.subcategory.options = options
        self.subcategory.value = selected or None
        self.subcategory.update()

    def _toggle_more(self) -> None:
        self.more_open = not self.more_open
        self.more_panel.set_visibility(self.more_open)

    def _kind_changed(self, event) -> None:
        is_settlement = event.value == SETTLEMENT_KIND
        self.kind_help.set_visibility(is_settlement)
        if self.identifier is None:
            self.heading.text = translate("Add settlement") if is_settlement else translate("Add expense")

    def _shift_date(self, days: int) -> None:
        try:
            current = date.fromisoformat(self.date.value or "")
        except ValueError:
            current = date.today()
        self.date.value = (current + timedelta(days=days)).isoformat()
        set_field_error(self.date_error)

    def _clear_errors(self) -> None:
        for label in (
            self.amount_error, self.date_error, self.description_error,
            self.category_error, self.error,
        ):
            set_field_error(label)

    def save(self, *_args, continue_entry: bool = True) -> None:
        self._clear_errors()
        amount = validated_value(positive_amount, self.amount_error, self.amount.value, "Amount")
        transaction_date = validated_value(required_date, self.date_error, self.date.value)
        description = validated_value(
            required_text, self.description_error, self.description.value, "Description",
        )
        if not self.selected_category:
            set_field_error(self.category_error, "Choose a category.")

        if any((amount is None, transaction_date is None, description is None,
                not self.selected_category)):
            return

        item = TransactionInput(
            date=transaction_date,
            amount=amount,
            description=description,
            category=self.selected_category,
            subcategory=self.subcategory.value or "",
            purpose=self.purpose.value or "",
            expense_type=ANNUAL_EXPENSE_TYPE if self.annual_allocation.value else "Living",
            need_want=self.existing_need_want,
            notes=self.notes.value or "",
            transaction_kind=self.transaction_kind.value or EXPENSE_KIND,
            account_id=int(self.account.value) if self.account.value else None,
        )
        noun = "Settlement" if item.transaction_kind == SETTLEMENT_KIND else "Expense"
        was_editing = self.identifier is not None
        try:
            if was_editing:
                self.repository.update(self.identifier, item)
            else:
                self.repository.add(item)
                ENTRY_DEFAULTS["date"] = item.date.isoformat()
                ENTRY_DEFAULTS["account_id"] = item.account_id or 0
        except ValueError as error:
            set_field_error(self.error, f"Could not save {noun.lower()}: {error}")
            return
        except AuthorizationExpired:
            raise
        except Exception:
            set_field_error(
                self.error,
                f"The {noun.lower()} could not be saved. Your data was not changed.",
            )
            return
        self._finish_save(
            continue_entry=continue_entry and not was_editing,
            message=f"{noun} updated" if was_editing else f"{noun} saved",
        )

    def _finish_save(
        self, *, continue_entry: bool, message: str, color: str = "positive",
    ) -> None:
        self.dialog.close()
        ui.notify(message, color=color, position="bottom", timeout=1800)
        self.on_saved()
        if continue_entry:
            ui.timer(0.12, self.open, once=True)


class IncomeEditor:
    def __init__(self, repository: Repository, on_saved):
        self.repository = repository
        self.on_saved = on_saved
        with ui.dialog() as self.dialog, ui.card().classes("small-editor-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                ui.label(translate("Add income")).classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body gap-3 w-full"):
                with ui.column().classes("gap-0 w-full"):
                    form_label("Amount", required=True)
                    self.amount = ui.input(placeholder="0.00").props(
                        "outlined dense prefix='$' inputmode=decimal"
                    ).classes("w-full")
                    self.amount_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Description", required=True)
                    self.description = ui.input(placeholder=translate("Salary, refund, freelance")).props(
                        "outlined dense"
                    ).classes("w-full")
                    self.description_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Date", required=True)
                    self.date = ui.input().props("outlined dense type=date").classes("w-full")
                    self.date_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Notes", hint="Optional")
                    self.notes = ui.input().props("outlined dense").classes("w-full")
                self.error = field_error()
            with ui.row().classes("editor-actions justify-end w-full"):
                ui.button(translate("Save income"), on_click=self.save).props("unelevated no-caps").classes("primary")
        self.amount.on_value_change(lambda _: set_field_error(self.amount_error))
        self.description.on_value_change(lambda _: set_field_error(self.description_error))
        self.date.on_value_change(lambda _: set_field_error(self.date_error))

    def open(self, month: str) -> None:
        today = date.today().isoformat()
        self.amount.value = ""
        self.description.value = ""
        self.date.value = today if today.startswith(month) else f"{month}-01"
        self.notes.value = ""
        self._clear_errors()
        self.dialog.open()
        ui.timer(0.1, lambda: self.amount.run_method("focus"), once=True)

    def save(self) -> None:
        self._clear_errors()
        amount = validated_value(positive_amount, self.amount_error, self.amount.value, "Amount")
        income_date = validated_value(required_date, self.date_error, self.date.value)
        description = validated_value(
            required_text, self.description_error, self.description.value, "Description",
        )
        if amount is None or income_date is None or description is None:
            return
        try:
            self.repository.add_income(IncomeInput(
                income_date, amount, description, self.notes.value or "",
            ))
        except ValueError as error:
            set_field_error(self.error, f"Could not save income: {error}")
            return
        except AuthorizationExpired:
            raise
        except Exception:
            set_field_error(self.error, "The income entry could not be saved. Your data was not changed.")
            return
        self.dialog.close()
        ui.notify("Income saved", color="positive", position="bottom", timeout=1500)
        self.on_saved()

    def _clear_errors(self) -> None:
        for label in (self.amount_error, self.description_error, self.date_error, self.error):
            set_field_error(label)


class NetWorthEditor:
    def __init__(self, repository: Repository, on_saved):
        self.repository = repository
        self.on_saved = on_saved
        with ui.dialog() as self.dialog, ui.card().classes("small-editor-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                ui.label(translate("Update net worth")).classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body gap-3 w-full"):
                with ui.column().classes("gap-0 w-full"):
                    form_label("Assets", required=True)
                    self.assets = ui.input(placeholder="0.00").props(
                        "outlined dense prefix='$' inputmode=decimal"
                    ).classes("w-full")
                    self.assets_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Liabilities", hint="Optional · defaults to $0")
                    self.liabilities = ui.input(placeholder="0.00").props(
                        "outlined dense prefix='$' inputmode=decimal"
                    ).classes("w-full")
                    self.liabilities_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Snapshot date", required=True)
                    self.date = ui.input().props("outlined dense type=date").classes("w-full")
                    self.date_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Notes", hint="Optional")
                    self.notes = ui.input().props("outlined dense").classes("w-full")
                self.error = field_error()
            with ui.row().classes("editor-actions justify-end w-full"):
                ui.button(translate("Save snapshot"), on_click=self.save).props("unelevated no-caps").classes("primary")
        self.assets.on_value_change(lambda _: set_field_error(self.assets_error))
        self.liabilities.on_value_change(lambda _: set_field_error(self.liabilities_error))
        self.date.on_value_change(lambda _: set_field_error(self.date_error))

    def open(self, dashboard: dict) -> None:
        snapshot = dashboard.get("net_worth")
        assets = snapshot.get("assets_cents") if snapshot else None
        liabilities = snapshot.get("liabilities_cents") if snapshot else None
        if snapshot and snapshot.get("estimated"):
            assets = snapshot.get("actual_assets_cents")
            liabilities = snapshot.get("actual_liabilities_cents")
        self.assets.value = f"{assets / 100:.2f}" if assets is not None else ""
        self.liabilities.value = f"{liabilities / 100:.2f}" if liabilities is not None else "0.00"
        self.date.value = date.today().isoformat()
        self.notes.value = ""
        self._clear_errors()
        self.dialog.open()
        ui.timer(0.1, lambda: self.assets.run_method("focus"), once=True)

    def save(self) -> None:
        self._clear_errors()
        assets = validated_value(
            nonnegative_amount, self.assets_error, self.assets.value, "Assets",
        )
        liabilities = validated_value(
            nonnegative_amount, self.liabilities_error, self.liabilities.value,
            "Liabilities", required=False,
        )
        snapshot_date = validated_value(
            required_date, self.date_error, self.date.value, "Snapshot date",
        )
        if assets is None or liabilities is None or snapshot_date is None:
            return
        try:
            self.repository.save_net_worth(NetWorthInput(
                snapshot_date, assets, liabilities, self.notes.value or "",
            ))
        except ValueError as error:
            set_field_error(self.error, f"Could not save snapshot: {error}")
            return
        except AuthorizationExpired:
            raise
        except Exception:
            set_field_error(self.error, "The snapshot could not be saved. Your data was not changed.")
            return
        self.dialog.close()
        ui.notify("Net worth updated", color="positive", position="bottom", timeout=1500)
        self.on_saved()

    def _clear_errors(self) -> None:
        for label in (self.assets_error, self.liabilities_error, self.date_error, self.error):
            set_field_error(label)


class IncomeEstimateEditor:
    def __init__(self, repository: Repository, on_saved):
        self.repository = repository
        self.on_saved = on_saved
        self.month = ""
        with ui.dialog() as self.dialog, ui.card().classes("small-editor-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                ui.label(translate("Estimated income")).classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body gap-3 w-full"):
                ui.label(
                    "This is a planning value only. It never changes recorded income or cash flow."
                ).classes("muted text-sm")
                form_label("Amount", required=True)
                self.amount = ui.input(placeholder="0.00").props(
                    "outlined dense prefix='$' inputmode=decimal"
                ).classes("w-full")
                self.amount_error = field_error()
                self.history_note = ui.label("").classes("muted text-xs")
                self.error = field_error()
            with ui.row().classes("editor-actions items-center justify-between w-full"):
                self.reset_button = ui.button("Use calculated estimate", on_click=self.reset).props(
                    "flat no-caps color=grey-7"
                )
                ui.button(translate("Save"), on_click=self.save).props("unelevated no-caps").classes("primary")

    def open(self, month: str, estimate: dict) -> None:
        self.month = month
        amount = estimate.get("amount_cents")
        self.amount.value = f"{amount / 100:.2f}" if amount is not None else ""
        observations = estimate.get("observations", 0)
        self.history_note.text = (
            f"Calculated from {observations} prior recorded month{'s' if observations != 1 else ''}."
            if observations else "No prior recorded income is available yet."
        )
        self.reset_button.set_visibility(bool(estimate.get("is_override") and estimate.get("calculated_cents") is not None))
        set_field_error(self.amount_error)
        set_field_error(self.error)
        self.dialog.open()
        ui.timer(0.1, lambda: self.amount.run_method("focus"), once=True)

    def save(self) -> None:
        value = validated_value(nonnegative_amount, self.amount_error, self.amount.value, "Estimated income")
        if value is None:
            return
        try:
            self.repository.save_income_estimate(IncomeEstimateInput(self.month, value))
        except AuthorizationExpired:
            raise
        except Exception as error:
            set_field_error(self.error, f"Could not save estimate: {error}")
            return
        self.dialog.close()
        self.on_saved()
        ui.notify("Income estimate saved", color="positive", position="bottom")

    def reset(self) -> None:
        try:
            self.repository.clear_income_estimate(self.month)
        except AuthorizationExpired:
            raise
        except Exception as error:
            set_field_error(self.error, f"Could not reset estimate: {error}")
            return
        self.dialog.close()
        self.on_saved()
        ui.notify("Calculated estimate restored", position="bottom")


class LiabilityEditor:
    def __init__(self, repository: Repository, on_saved):
        self.repository = repository
        self.on_saved = on_saved
        self.identifier: int | None = None
        self.payment_labels: dict[str, str] = {}
        with ui.dialog() as self.dialog, ui.card().classes("editor-card liability-editor-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                self.heading = ui.label("Add loan").classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body gap-3 w-full"):
                with ui.row().classes("w-full gap-3 entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Loan name", required=True)
                        self.name = ui.input(placeholder="Home mortgage").props("outlined dense").classes("w-full")
                        self.name_error = field_error()
                    with ui.column().classes("gap-0 col"):
                        form_label("Type", required=True)
                        self.kind = ui.select(translated_options(LIABILITY_TYPES), value="Mortgage").props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("w-full")
                with ui.row().classes("w-full gap-3 entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Original principal", required=True)
                        self.principal = ui.input().props("outlined dense prefix='$' inputmode=decimal").classes("w-full")
                        self.principal_error = field_error()
                    with ui.column().classes("gap-0 col"):
                        form_label("Annual interest rate", required=True)
                        self.rate = ui.input().props("outlined dense suffix='%' inputmode=decimal").classes("w-full")
                        self.rate_error = field_error()
                with ui.row().classes("w-full gap-3 entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Current balance", required=True)
                        self.current_balance = ui.input().props(
                            "outlined dense prefix='$' inputmode=decimal"
                        ).classes("w-full")
                        self.current_balance_error = field_error()
                    with ui.column().classes("gap-0 col"):
                        form_label("Balance as of", required=True)
                        self.balance_as_of = ui.input().props(
                            "outlined dense type=date"
                        ).classes("w-full")
                        self.balance_as_of_error = field_error()
                with ui.row().classes("w-full gap-3 entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Rate type", required=True)
                        self.rate_type = ui.select(
                            translated_options(MORTGAGE_RATE_TYPES), value="Fixed",
                        ).props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("w-full")
                    with ui.column().classes("gap-0 col"):
                        form_label("Interest convention", required=True)
                        self.interest_convention = ui.select(
                            translated_options(INTEREST_CONVENTIONS), value="Canadian semi-annual",
                        ).props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("w-full")
                        self.kind.on_value_change(self._liability_type_changed)
                with ui.row().classes("w-full gap-3 entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Remaining amortization years", required=True)
                        self.years = ui.number(min=1, max=100, step=1).props("outlined dense").classes("w-full")
                        self.years_error = field_error()
                    with ui.column().classes("gap-0 col"):
                        form_label("Current rate term years", required=True)
                        self.rate_term_years = ui.number(min=1, max=100, step=1).props(
                            "outlined dense"
                        ).classes("w-full")
                        self.rate_term_error = field_error()
                    with ui.column().classes("gap-0 col"):
                        form_label("Original first payment date", required=True)
                        self.start_date = ui.input().props("outlined dense type=date").classes("w-full")
                        self.start_error = field_error()
                with ui.row().classes("w-full gap-3 entry-primary-row"):
                    with ui.column().classes("gap-0 col"):
                        form_label("Payment frequency", required=True)
                        self.payment_frequency = ui.select(
                            translated_options(PAYMENT_FREQUENCIES), value="Monthly",
                        ).props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("w-full")
                    with ui.column().classes("gap-0 col"):
                        form_label("Current payment", hint="Optional · calculated if blank")
                        self.payment_amount = ui.input().props(
                            "outlined dense prefix='$' inputmode=decimal"
                        ).classes("w-full")
                        self.payment_error = field_error()
                with ui.column().classes("gap-0 w-full"):
                    form_label("Payment transactions", hint="Optional")
                    self.payment_match = ui.select(
                        {"": "Not linked"}, value="",
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("w-full")
                    ui.label(
                        "Link a recurring imported debit once; future matching payments are picked up automatically."
                    ).classes("muted text-xs mt-1")
                form_label("Notes", hint="Optional")
                self.notes = ui.input().props("outlined dense").classes("w-full")
                ui.label(
                    "The current balance and date anchor all calculations. A blank payment is calculated from that balance, the remaining amortization, frequency, and entered rate; no current rate is applied retroactively."
                ).classes("muted text-xs")
                self.error = field_error()
            with ui.row().classes("editor-actions justify-end w-full"):
                ui.button(translate("Save"), on_click=self.save).props("unelevated no-caps").classes("primary")
        self.principal.on("blur", self._default_current_balance)

    def open(self, record: dict | None = None) -> None:
        self.identifier = record.get("id") if record else None
        self.heading.text = "Edit loan" if record else "Add loan"
        self.name.value = record.get("name", "") if record else ""
        self.kind.value = record.get("liability_type", "Mortgage") if record else "Mortgage"
        self.principal.value = f'{record["original_principal_cents"] / 100:.2f}' if record else ""
        self.current_balance.value = (
            f'{record["current_balance_cents"] / 100:.2f}' if record else ""
        )
        self.balance_as_of.value = (
            record.get("balance_as_of_date", date.today().isoformat())
            if record else date.today().isoformat()
        )
        self.rate.value = f'{record["annual_rate_bps"] / 100:.2f}' if record else ""
        self.rate_type.value = record.get("rate_type", "Fixed") if record else "Fixed"
        self.interest_convention.value = (
            record.get("interest_convention", "Canadian semi-annual")
            if record else "Canadian semi-annual"
        )
        self.years.value = record["term_months"] / 12 if record else 25
        self.rate_term_years.value = record.get("rate_term_months", 60) / 12 if record else 5
        self.payment_frequency.value = record.get("payment_frequency", "Monthly") if record else "Monthly"
        self.payment_amount.value = f'{record["payment_cents"] / 100:.2f}' if record else ""
        self.start_date.value = record.get("start_date", date.today().isoformat()) if record else date.today().isoformat()
        candidates = self.repository.recurring_payment_candidates()
        self.payment_labels = {item["match_key"]: item["label"] for item in candidates}
        options = {"": "Not linked"}
        options.update({
            item["match_key"]: (
                f'{item["label"]} · {item["uses"]} payments · '
                f'${item["average_cents"] / 100:,.2f} average'
            )
            for item in candidates
        })
        existing_key = record.get("payment_match_key", "") if record else ""
        if existing_key and existing_key not in options:
            options[existing_key] = record.get("payment_match_label") or existing_key.split(":", 1)[-1]
            self.payment_labels[existing_key] = options[existing_key]
        self.payment_match.options = options
        self.payment_match.value = existing_key
        self.payment_match.update()
        self.notes.value = record.get("notes", "") if record else ""
        for label in (
            self.name_error, self.principal_error, self.current_balance_error,
            self.balance_as_of_error, self.rate_error, self.years_error,
            self.rate_term_error, self.start_error, self.payment_error, self.error,
        ):
            set_field_error(label)
        self.dialog.open()
        ui.timer(0.1, lambda: self.name.run_method("focus"), once=True)

    def _liability_type_changed(self, event) -> None:
        self.interest_convention.value = (
            "Canadian semi-annual" if event.value == "Mortgage" else "Monthly"
        )

    def _default_current_balance(self, _) -> None:
        if not str(self.current_balance.value or "").strip():
            self.current_balance.value = self.principal.value or ""

    def save(self) -> None:
        name = validated_value(required_text, self.name_error, self.name.value, "Loan name")
        principal = validated_value(positive_amount, self.principal_error, self.principal.value, "Original principal")
        current_balance = validated_value(
            positive_amount, self.current_balance_error,
            self.current_balance.value, "Current balance",
        )
        balance_as_of = validated_value(
            required_date, self.balance_as_of_error, self.balance_as_of.value,
            "Balance as of",
        )
        rate = validated_value(nonnegative_amount, self.rate_error, self.rate.value, "Annual interest rate")
        start = validated_value(required_date, self.start_error, self.start_date.value, "First payment date")
        payment = None
        if str(self.payment_amount.value or "").strip():
            payment = validated_value(
                positive_amount, self.payment_error,
                self.payment_amount.value, "Current payment",
            )
        try:
            years = int(self.years.value or 0)
            if years < 1:
                raise ValueError
        except (TypeError, ValueError):
            set_field_error(self.years_error, "Remaining amortization years is required.")
            years = 0
        try:
            rate_term_years = int(self.rate_term_years.value or 0)
            if rate_term_years < 1 or (years and rate_term_years > years):
                raise ValueError
        except (TypeError, ValueError):
            set_field_error(
                self.rate_term_error,
                "Rate term must be at least one year and no longer than the amortization.",
            )
            rate_term_years = 0
        if (
            None in (name, principal, current_balance, balance_as_of, rate, start)
            or (str(self.payment_amount.value or "").strip() and payment is None)
            or not years or not rate_term_years
        ):
            return
        try:
            self.repository.save_liability(LiabilityInput(
                name=name, liability_type=self.kind.value, original_principal=principal,
                annual_rate_percent=rate, term_months=years * 12, start_date=start,
                notes=self.notes.value or "",
                payment_match_key=self.payment_match.value or "",
                payment_match_label=(
                    self.payment_labels.get(self.payment_match.value, "")
                    if self.payment_match.value else ""
                ),
                rate_type=self.rate_type.value,
                interest_convention=self.interest_convention.value,
                rate_term_months=rate_term_years * 12,
                current_balance=current_balance,
                balance_as_of=balance_as_of,
                payment_frequency=self.payment_frequency.value,
                payment_amount=payment,
            ), self.identifier)
        except AuthorizationExpired:
            raise
        except Exception as error:
            set_field_error(self.error, f"Could not save loan: {error}")
            return
        self.dialog.close()
        self.on_saved()
        ui.notify("Loan saved", color="positive", position="bottom")
