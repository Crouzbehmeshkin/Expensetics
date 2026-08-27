from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
import os
from pathlib import Path
import sys
from urllib.parse import quote

from nicegui import app, core, native, ui
from starlette.responses import PlainTextResponse

from .charts import (
    category_activity_chart, compact_detail_bar, liability_balance_chart,
    liability_payment_chart, net_worth_chart, ranked_total_bar,
    selectable_category_bar, settlement_activity_chart, stacked_area_chart,
    transaction_count_chart, weekday_activity_chart,
)
from .bank_import_ui import BankImportDialog
from .components import brand_mark, chart_title, chip_button, primary_action
from .db import DATA_DIR, DB_PATH, connect, initialize, transaction
from .device_unlock import (
    DeviceUnlockError, DeviceUnlockStore, platform_authenticator_name, result_error,
    unlock_script,
)
from .formatting import account_label, date_label, money, month_label
from .forms import (
    AccountEditor, ExpenseEditor, IncomeEditor, IncomeEstimateEditor, LiabilityEditor,
    NetWorthEditor,
)
from .i18n import (
    configure_document, current_language, language_options, set_language, translate,
)
from .import_policy import oversized_upload_request
from .repository import Repository
from .settings_ui import SettingsDialog
from .services import ANNUAL_EXPENSE_TYPE, SETTLEMENT_KIND, shifted_month, validate_month
from .session_security import (
    AuthorizationExpired, authorize_session, has_authorized_sessions, local_host,
    LoopbackHostMiddleware, revoke_all_sessions, revoke_session,
    session_is_authorized, session_permit, storage_secret, touch_session,
)
from .state import ENTRY_DEFAULTS
from .vault import (
    VaultError, database_state, is_unlocked, lock as lock_vault, prepare,
    recover_encrypted_candidate, recover_interrupted_migration, remove_legacy_csvs,
    unlock,
)

CSS_PATH = Path(__file__).parent / "styles" / "app.css"
ASSET_PATH = Path(__file__).parent / "assets"

# NiceGUI permits cross-origin Socket.IO connections by default. Expensetics has
# no remote embedding mode, so retain Engine.IO's same-origin validation and
# reject non-loopback Host headers before either HTTP or WebSocket dispatch.
core.sio.eio.cors_allowed_origins = None
app.add_middleware(LoopbackHostMiddleware)
app.add_static_files("/assets", ASSET_PATH)


@app.middleware("http")
async def bound_upload_requests(request, call_next):
    if oversized_upload_request(
        request.method, request.url.path, request.headers.get("content-length"),
    ):
        return PlainTextResponse("Upload exceeds the local safety limit", status_code=413)
    return await call_next(request)


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def selected_month(explicit: str | None = None) -> str:
    candidate = explicit or app.storage.user.get("selected_month") or current_month()
    try:
        validate_month(candidate)
    except (TypeError, ValueError):
        candidate = current_month()
    app.storage.user["selected_month"] = candidate
    return candidate


def remember_month(value: str) -> None:
    validate_month(value)
    app.storage.user["selected_month"] = value


def entry_count(value: int) -> str:
    return translate("{count} entry" if value == 1 else "{count} entries", count=value)


def add_global_styles() -> None:
    ui.add_css(CSS_PATH.read_text(encoding="utf-8"))
    ui.add_head_html('<script src="/assets/device_unlock.js"></script>')
    configure_document()


def session_is_unlocked() -> bool:
    return is_unlocked() and session_is_authorized(app.storage.user)


def require_unlocked() -> bool:
    if session_is_unlocked():
        return True
    ui.navigate.to("/unlock")
    return False


def session_repository() -> Repository:
    """Bind all page callbacks to the current server-issued session permit."""
    permit = session_permit(app.storage.user)
    if permit is None or not session_is_authorized(app.storage.user):
        raise AuthorizationExpired("This session is locked")

    def handle_expired_session(error: Exception) -> None:
        if isinstance(error, AuthorizationExpired):
            ui.navigate.to("/unlock")

    ui.on_exception(handle_expired_session)
    return Repository(permit=permit)


def finish_unlock() -> Path | None:
    permit = authorize_session(app.storage.user)
    repository = Repository(permit=permit)
    try:
        initialize(permit=permit)
        with connect(permit=permit) as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key='csv_export_directory'"
            ).fetchone()
        external_legacy_directory = None
        if row and row["value"]:
            configured = Path(row["value"]).expanduser().resolve()
            if configured != DATA_DIR.resolve() and configured.is_dir():
                external_legacy_directory = configured
        remove_legacy_csvs(DATA_DIR)
        with transaction(permit=permit) as connection:
            connection.execute("DELETE FROM app_settings WHERE key='csv_export_directory'")
        ENTRY_DEFAULTS["date"] = repository.last_manual_date() or date.today().isoformat()
        ENTRY_DEFAULTS["account_id"] = repository.last_manual_account_id() or 0
        return external_legacy_directory
    except Exception:
        revoke_session(app.storage.user)
        lock_vault()
        raise


@ui.page("/unlock")
def unlock_page() -> None:
    add_global_styles()
    recovery_error = ""
    try:
        recover_interrupted_migration(DB_PATH)
        state = database_state(DB_PATH)
    except VaultError as issue:
        state = "recovery_required"
        recovery_error = str(issue)
    recovery_required = state == "recovery_required"
    setup = state in {"missing", "plaintext"}
    with ui.column().classes("vault-page"):
        with ui.column().classes("vault-card"):
            with ui.row().classes("items-center gap-3"):
                brand_mark()
                with ui.column().classes("gap-0"):
                    ui.label("Expensetics").classes("brand")
                    ui.label(
                        translate("Recover your encrypted vault") if recovery_required
                        else translate("Create your encrypted vault") if setup
                        else translate("Unlock your encrypted vault")
                    ).classes("section-title")
            if recovery_required:
                copy = (
                    "Encryption was interrupted after the original database was moved. "
                    "Enter its password to verify and recover the encrypted candidate."
                )
            elif setup:
                copy = "Choose an app password. It encrypts the database and is not stored by Expensetics."
            else:
                copy = (
                    "Enter your app password. It directly unlocks the encrypted database and is never stored in readable form."
                )
            ui.label(copy).classes("muted text-sm vault-copy")
            password = ui.input(
                translate("App password"), password=True, password_toggle_button=True,
            ).props("outlined autofocus").classes("w-full")
            confirmation = None
            if setup:
                confirmation = ui.input(
                    translate("Confirm password"), password=True,
                ).props("outlined").classes("w-full")
                ui.label(translate("Required · at least 12 characters")).classes("muted text-xs")
            error = ui.label(recovery_error).classes("field-error")
            error.set_visibility(bool(recovery_error))

            device_store = DeviceUnlockStore(DATA_DIR)

            async def device_unlock() -> None:
                try:
                    record = device_store.load()
                    result = await ui.run_javascript(
                        unlock_script(record),
                        timeout=75.0,
                    )
                    message = result_error(result)
                    if message:
                        raise DeviceUnlockError(message)
                    device_password = result.pop("password")
                    try:
                        if recovery_required:
                            recover_encrypted_candidate(DB_PATH, device_password)
                        else:
                            unlock(DB_PATH, device_password)
                    finally:
                        device_password = ""
                    warning = finish_unlock()
                except (
                    DeviceUnlockError, KeyError, TimeoutError, ValueError, VaultError, OSError,
                ) as issue:
                    error.text = str(issue)
                    error.set_visibility(True)
                    return
                if warning:
                    ui.notify(
                        f"Legacy CSV files may remain in {warning}. They were not deleted automatically.",
                        color="warning", timeout=5000,
                    )
                ui.navigate.to("/")

            def submit() -> None:
                try:
                    if recovery_required:
                        recover_encrypted_candidate(DB_PATH, password.value or "")
                    elif setup:
                        prepare(DB_PATH, password.value or "", confirmation.value or "")
                    else:
                        unlock(DB_PATH, password.value or "")
                    warning = finish_unlock()
                except (ValueError, VaultError, OSError) as issue:
                    error.text = str(issue)
                    error.set_visibility(True)
                    return
                password.value = ""
                if confirmation:
                    confirmation.value = ""
                if warning:
                    ui.notify(
                        f"Legacy CSV files may remain in {warning}. They were not deleted automatically.",
                        color="warning", timeout=5000,
                    )
                ui.navigate.to("/")

            password.on("keydown.enter", submit)
            if confirmation:
                confirmation.on("keydown.enter", submit)
            primary_action(
                "Recover encrypted vault" if recovery_required
                else "Create encrypted vault" if setup else "Unlock",
                "lock_open", submit,
            ).classes("w-full vault-action")
            if not setup and device_store.is_enrolled():
                with ui.row().classes("vault-divider items-center w-full no-wrap"):
                    ui.separator()
                    ui.label(translate("or")).classes("muted text-xs")
                    ui.separator()
                ui.button(
                    translate("Unlock with {authenticator}").format(
                        authenticator=translate(platform_authenticator_name())
                    ),
                    icon="fingerprint",
                    on_click=device_unlock,
                ).props("outline no-caps").classes("w-full vault-action device-unlock-action")
            ui.label(
                "Keep the app password and an encrypted backup safe. Device unlock is not a portable recovery method."
            ).classes("muted text-xs")


def change_language(code: str) -> None:
    if code == current_language():
        return
    set_language(code)
    ui.run_javascript("window.location.reload()")


def language_menu() -> None:
    active_language = current_language()
    options = language_options()
    with ui.button(
        active_language.upper(), icon="language",
    ).props(
        f"flat dense no-caps aria-label='{translate('Language')}: "
        f"{options[active_language]}' title='{translate('Language')}'"
    ).classes("language-button"):
        with ui.menu().props("anchor='bottom right' self='top right'").classes(
            "language-menu"
        ):
            for code, native_name in options.items():
                with ui.menu_item(
                    on_click=lambda selected=code: change_language(selected),
                ).classes(
                    "language-menu-item" + (" active" if code == active_language else "")
                ):
                    ui.label(native_name)
                    if code == active_language:
                        ui.icon("check").classes("language-menu-check")


def lock_all_clients(*, redirect: bool = True) -> None:
    """Revoke data access before clearing the process key and visible pages."""
    revoke_all_sessions(app.storage.user)
    lock_vault()
    if redirect:
        for client in tuple(app.clients()):
            client.run_javascript("window.location.replace('/unlock')")


def header(
    repository: Repository, active: str, open_editor, on_data_changed,
) -> None:
    settings = SettingsDialog(repository, on_data_changed, lock_all_clients)
    with ui.header().classes("topbar items-center"):
        with ui.row().classes("shell app-header-layout items-center no-wrap"):
            with ui.row().classes("brand-cluster items-center gap-3 no-wrap"):
                brand_mark()
                ui.label("Expensetics").classes("brand")
            with ui.row().classes("gap-1 nav-links no-wrap"):
                ui.link(translate("Overview"), "/").classes(f"nav-button {'active' if active == 'overview' else ''}")
                ui.link(translate("Expenses"), "/expenses").classes(f"nav-button {'active' if active == 'expenses' else ''}")
                ui.link(translate("Insights"), "/insights").classes(f"nav-button {'active' if active == 'insights' else ''}")
                ui.link(translate("Budget"), "/budget").classes(f"nav-button {'active' if active == 'budget' else ''}")
                ui.link(translate("Accounts"), "/accounts").classes(f"nav-button {'active' if active == 'accounts' else ''}")
                ui.link(translate("Loans"), "/liabilities").classes(f"nav-button {'active' if active == 'liabilities' else ''}")
            with ui.row().classes("header-actions items-center gap-2 no-wrap"):
                language_menu()
                ui.button(icon="lock", on_click=lock_all_clients).props(
                    f"flat round dense aria-label='{translate('Lock app')}' "
                    f"title='{translate('Lock app')}'"
                ).classes("settings-button")
                ui.button(icon="settings", on_click=settings.open).props(
                    f"flat round dense aria-label='{translate('Settings')}' title='{translate('Settings')}'"
                ).classes("settings-button")
                primary_action("Add expense", "add", open_editor).props(
                    f"aria-label='{translate('Add expense')}' title='{translate('Add expense')}'"
                ).classes("header-add-action")


def month_control(value: str, on_change) -> None:
    with ui.row().classes("month-control items-center no-wrap"):
        ui.button(icon="chevron_left", on_click=lambda: on_change(shifted_month(value, -1))).props(
            f"flat round dense aria-label='{translate('Previous month')}'"
        )
        ui.input(value=value, on_change=lambda event: on_change(event.value)).props(
            f"borderless dense type=month aria-label='{translate('Choose month')}'"
        ).classes("month-input")
        ui.button(icon="chevron_right", on_click=lambda: on_change(shifted_month(value, 1))).props(
            f"flat round dense aria-label='{translate('Next month')}'"
        )


def insight_signal_copy(signal: dict) -> tuple[str, str, str, str, str]:
    """Translate one structured deterministic signal into concise UI copy."""
    kind = signal["kind"]
    if kind == "recurring_increase":
        percent = f'{signal["percent"]}%' if signal["percent"] is not None else money(
            signal["change_cents"]
        )
        return (
            translate("Recurring change"), "trending_up",
            translate("{merchant} increased {change}", merchant=signal["label"], change=percent),
            translate(
                "{current} now · usually {usual} · {category}",
                current=money(signal["current_cents"]),
                usual=money(signal["usual_cents"]),
                category=translate(signal["category"]),
            ),
            f'+{money(signal["change_cents"])}',
        )
    if kind == "amount_outlier":
        return (
            translate("Amount outlier"), "priority_high",
            translate(
                "{merchant} recorded an unusually {direction} amount",
                merchant=signal["label"], direction=translate(signal["direction"]),
            ),
            translate(
                "{amount} vs a historical median of {usual} · robust score {score}",
                amount=money(signal["amount_cents"]),
                usual=money(signal["usual_cents"]), score=f'{signal["score"]:.1f}',
            ),
            money(signal["amount_cents"]),
        )
    if kind == "timing_shift":
        return (
            translate("Timing shift"), "schedule",
            translate(
                "{merchant} posted {days} days {direction}",
                merchant=signal["label"], days=signal["days"],
                direction=translate(signal["direction"]),
            ),
            translate(
                "Day {current_day} this month · usually around day {expected_day} · {category}",
                current_day=signal["current_day"], expected_day=signal["expected_day"],
                category=translate(signal["category"]),
            ),
            translate("{days} days", days=signal["days"]),
        )
    if kind == "merchant_activity":
        return (
            translate("Activity change"), "repeat",
            translate("More activity at {merchant}", merchant=signal["label"]),
            translate(
                "{current_count} transactions this month · usually {usual_count} · {category}",
                current_count=signal["current_count"], usual_count=signal["usual_count"],
                category=translate(signal["category"]),
            ),
            f'+{signal["change_count"]}',
        )
    if kind == "category_activity":
        return (
            translate("Frequency change"), "bar_chart",
            translate(
                "{category} happened more often", category=translate(signal["category"]),
            ),
            translate(
                "{current_count} purchases this month · usually {usual_count} across the prior six months",
                current_count=signal["current_count"], usual_count=signal["usual_count"],
            ),
            f'+{signal["change_count"]}',
        )
    if kind == "recurring_stable":
        return (
            translate("Recurring rhythm"), "event_available",
            translate("Recurring charges stayed predictable"),
            translate(
                "Stable date and amount: {stable_count} of {total_count} recurring charges",
                stable_count=signal["stable_count"], total_count=signal["total_count"],
            ),
            translate("Stable: {stable_count}", stable_count=signal["stable_count"]),
        )
    if kind == "settlement_activity":
        return (
            translate("Settlements"), "handshake",
            translate("Settlements reduced this month’s spending"),
            translate(
                "Settlements assigned to expense categories: {count}",
                count=signal["count"],
            ),
            money(signal["total_cents"]),
        )
    raise ValueError(f"Unsupported insight signal: {kind}")

def install_shortcuts(
    editor: ExpenseEditor,
    on_month_shift: Callable[[int], None] | None = None,
) -> None:
    ui.on("session-activity", lambda: touch_session(app.storage.user))

    def enforce_expiry() -> None:
        if not session_is_unlocked():
            if not has_authorized_sessions():
                lock_vault()
            ui.navigate.to("/unlock")

    ui.timer(15.0, enforce_expiry)
    ui.on("open-expense", lambda: editor.open())
    if on_month_shift:
        ui.on("previous-overview-month", lambda: on_month_shift(-1))
        ui.on("next-overview-month", lambda: on_month_shift(1))
    ui.run_javascript("""
        if (!window.__expenseticsShortcuts) {
          window.__expenseticsShortcuts = true;
          let lastActivitySignal = 0;
          const recordActivity = () => {
            const now = Date.now();
            if (now - lastActivitySignal >= 30000) {
              lastActivitySignal = now;
              emitEvent('session-activity');
            }
          };
          ['keydown', 'pointerdown', 'pointermove', 'wheel', 'touchstart'].forEach(
            eventName => window.addEventListener(eventName, recordActivity, {passive: true})
          );
          window.addEventListener('keydown', (event) => {
            const tag = document.activeElement?.tagName?.toLowerCase();
            const typing = tag === 'input' || tag === 'textarea' || tag === 'select'
              || document.activeElement?.isContentEditable;
            const modified = event.metaKey || event.ctrlKey || event.altKey || event.shiftKey;
            if (!typing && !modified && event.key.toLowerCase() === 'n') {
              event.preventDefault(); emitEvent('open-expense');
            }
            const dialogOpen = document.querySelector('.q-dialog--modal');
            if (!typing && !modified && !dialogOpen && window.location.pathname === '/') {
              if (event.key === 'ArrowLeft') {
                event.preventDefault(); emitEvent('previous-overview-month');
              } else if (event.key === 'ArrowRight') {
                event.preventDefault(); emitEvent('next-overview-month');
              }
            }
          });
        }
    """)


def compact_expense(record: dict, on_edit) -> None:
    with ui.element("div").classes("compact-expense").on("click", lambda: on_edit(record)):
        with ui.column().classes("gap-0 min-w-0"):
            ui.label(record["description"]).classes("font-medium ellipsis")
            detail = date_label(record["date"])
            if record.get("subcategory"):
                detail += f" · {record['subcategory']}"
            ui.label(detail).classes("muted text-xs")
        ui.label(money(record["amount_cents"])).classes("amount")


@ui.page("/")
def overview_page() -> None:
    if not require_unlocked():
        return
    add_global_styles()
    repo = session_repository()
    state = {"month": selected_month(), "category": None}

    def refresh() -> None:
        overview.refresh()

    def set_month(value: str) -> None:
        remember_month(value)
        state["month"] = value
        refresh()

    def select_category(value: str) -> None:
        state["category"] = value
        refresh()

    editor = ExpenseEditor(repo, refresh)
    income_editor = IncomeEditor(repo, refresh)
    estimate_editor = IncomeEstimateEditor(repo, refresh)
    net_worth_editor = NetWorthEditor(repo, refresh)
    header(repo, "overview", editor.open, refresh)
    install_shortcuts(
        editor,
        lambda offset: set_month(shifted_month(state["month"], offset)),
    )

    @ui.refreshable
    def overview() -> None:
        with repo.read_session() as reader:
            category_trends = reader.category_trend(state["month"], count=12)
            total_budget_trend = reader.total_budget_trend(state["month"], count=12)
            summary = reader.dashboard(state["month"], category_trend=category_trends)
            category_names = [series["name"] for series in category_trends["series"]]
            if state["category"] not in category_names:
                state["category"] = None
            subcategory_types = (
                reader.subcategory_type_breakdown(state["month"], state["category"])
                if state["category"] else {"categories": [], "series": []}
            )
            budget_rows = {
                row["category"]: row
                for row in reader.budgets(state["month"], category_trend=category_trends)
                if row["amount_cents"] > 0
            }
            net_worth_history = reader.net_worth_trend()
        current_equivalent = {
            series["name"]: series["values"][-1]
            for series in category_trends["series"]
        }
        chart_categories = [
            category for category in category_names
            if current_equivalent.get(category, 0) or category in budget_rows
        ]
        category_types = {
            "categories": chart_categories,
            "series": [{
                "name": "Spending",
                "values": [current_equivalent.get(category, 0) for category in chart_categories],
            }],
        }
        movers = repo.category_movers_from_trend(category_trends)
        with ui.column().classes("shell gap-5 pb-12"):
            with ui.row().classes("page-head items-end justify-between w-full page-head-row"):
                with ui.column().classes("gap-2"):
                    ui.label(month_label(state["month"])).classes("eyebrow")
                    ui.label(money(summary["normalized"])).classes("hero-total")
                    comparison_text = translate("Monthly cost equivalent")
                    if summary["previous_normalized"]:
                        delta = (
                            (summary["normalized"] - summary["previous_normalized"])
                            / summary["previous_normalized"] * 100
                        )
                        comparison_text = translate(
                            "{percent}% more than the prior month" if delta >= 0
                            else "{percent}% less than the prior month",
                            percent=f"{abs(delta):.0f}",
                        )
                    ui.label(comparison_text).classes("muted text-sm")
                month_control(state["month"], set_month)

            with ui.element("section").classes("panel financial-grid w-full"):
                financial_items = (
                    ("Outgoing", summary["outgoing"]),
                    ("Income", summary["display_income"]),
                    ("Net cash flow", summary["display_net_cashflow"]),
                    ("Net worth", summary["net_worth"]["net_worth"] if summary["net_worth"] else None),
                )
                for label, value in financial_items:
                    with ui.column().classes("financial-cell gap-1"):
                        ui.label(translate(label)).classes("muted text-xs")
                        ui.label(money(value) if value is not None else translate("Not set")).classes(
                            f"metric {'negative' if value is not None and value < 0 else ''}"
                        )
                        if label == "Net worth" and summary["net_worth"] and summary["net_worth"].get("estimated"):
                            actual_date = datetime.fromisoformat(
                                summary["net_worth"]["actual_date"]
                            ).strftime("%b %d, %Y")
                            ui.label(
                                f"Estimated from recorded cash flow since {actual_date}"
                            ).classes("estimate-note")
                        if label == "Income":
                            estimate = summary["income_estimate"]
                            if summary["income_is_estimated"]:
                                if estimate["is_override"]:
                                    explanation = translate("Manual planning estimate")
                                elif estimate["observations"] == 1:
                                    explanation = translate(
                                        "Estimated from one prior income month"
                                    )
                                else:
                                    explanation = translate(
                                        "Estimated from {count} prior income months",
                                        count=estimate["observations"],
                                    )
                                ui.label(explanation).classes("estimate-note")
                            elif estimate["amount_cents"] is None and not summary["income"]:
                                ui.label(
                                    translate(
                                        "No estimate yet · add recorded income to establish a baseline"
                                    )
                                ).classes("estimate-note")
                            else:
                                ui.label(translate("Recorded income")).classes("estimate-note")
                            with ui.row().classes("gap-1 income-actions no-wrap"):
                                ui.button(translate("Add"), on_click=lambda: income_editor.open(state["month"])).props(
                                    "flat dense no-caps"
                                ).classes("inline-action")
                                ui.button(translate("Edit estimate"), on_click=lambda: estimate_editor.open(
                                    state["month"], summary["income_estimate"]
                                )).props("flat dense no-caps").classes("inline-action")
                        elif label == "Net cash flow" and summary["income_is_estimated"]:
                            ui.label(translate("Projected using estimated income")).classes(
                                "estimate-note"
                            )
                        elif label == "Net worth":
                            ui.button(translate("Update"), on_click=lambda: net_worth_editor.open(summary)).props(
                                "flat dense no-caps"
                            ).classes("inline-action")

            with ui.element("section").classes("panel summary-grid w-full"):
                spending_items = (
                    ("Regular purchases", summary["regular"]),
                    ("Annual expenses", summary["annual"]),
                    ("Monthly equivalent", summary["normalized"]),
                    ("Transactions", summary["transaction_count"]),
                )
                for label, value in spending_items:
                    with ui.column().classes("summary-cell gap-1"):
                        ui.label(translate(label)).classes("muted text-xs")
                        ui.label(str(value) if label == "Transactions" else money(value)).classes("metric")

            with ui.element("section").classes("overview-breakdown-grid w-full"):
                with ui.column().classes("panel p-5 gap-1 min-w-0"):
                    with ui.row().classes("items-start justify-between w-full gap-4 mb-3"):
                        chart_title(
                            "Category breakdown",
                            "Monthly-equivalent spending by category. Click a bar for detail."
                            + (" Red markers show budget limits." if budget_rows else ""),
                        )
                        ui.link(translate("Explore in Insights"), "/insights").classes("text-link")
                    if category_types["categories"]:
                        selectable_category_bar(
                            category_types,
                            state["category"],
                            select_category,
                            budgets=budget_rows,
                        )
                    else:
                        ui.label("No expenses yet for this month. Press N to add your first one.").classes("empty")

                with ui.column().classes("panel p-5 gap-1 min-w-0"):
                    if state["category"]:
                        detail_total = sum(
                            subcategory_types["series"][0]["values"]
                            if subcategory_types.get("series") else []
                        )
                        chart_title(
                            f'{translate(state["category"])} {translate("Category detail")}',
                            "Subcategories first; vendor names fill only missing subcategories.",
                        )
                        ui.label(money(detail_total)).classes("breakdown-detail-total")
                        if subcategory_types["categories"]:
                            compact_detail_bar(subcategory_types)
                        else:
                            ui.label("No subcategory or vendor detail is available.").classes(
                                "empty compact"
                            )
                    else:
                        chart_title(
                            "Subcategory breakdown",
                            "Select a category on the left to inspect its detail.",
                        )
                        with ui.element("div").classes("breakdown-placeholder"):
                            ui.label(translate("Select a category")).classes("font-medium")
                            ui.label("Its subcategories and vendor fallbacks will appear here.").classes(
                                "muted text-xs"
                            )

            with ui.element("section").classes("panel p-5 w-full"):
                with ui.row().classes("items-start justify-between w-full gap-4 comparison-heading"):
                    trend_support = translate(
                        "Five largest 12-month category contributors; the remainder is grouped without losing value."
                    )
                    if any(value and value > 0 for value in total_budget_trend["values"]):
                        trend_support += " " + translate(
                            "The dashed red line is the explicitly set total monthly limit."
                        )
                    chart_title(
                        "Where spending moved",
                        trend_support,
                    )
                    ui.link(translate("Open Insights"), "/insights").classes("text-link")
                if category_trends["series"]:
                    stacked_area_chart(
                        category_trends, height=350, other_label="Other categories",
                        translate_names=True,
                        budget_values=total_budget_trend["values"],
                    )
                else:
                    ui.label("Add expenses in more than one month to reveal a trend.").classes("empty compact")

            with ui.element("section").classes("category-detail-grid w-full"):
                with ui.column().classes("panel p-5 gap-2"):
                    with ui.row().classes("items-start justify-between w-full"):
                        chart_title(
                            "Net worth",
                            "Actual snapshots plus estimates calculated as the last actual net worth + recorded income − recorded expenses.",
                        )
                        ui.button(translate("Update"), on_click=lambda: net_worth_editor.open(summary)).props("flat dense no-caps").classes("inline-chart-action")
                    if net_worth_history:
                        net_worth_chart(net_worth_history)
                    else:
                        ui.label("Add your first net-worth snapshot to start the line.").classes("empty compact")
                with ui.column().classes("panel p-5 gap-3"):
                    chart_title("Biggest monthly shifts", "The categories that changed most from the previous month.")
                    if movers:
                        for item in movers:
                            with ui.element("div").classes("mover-row"):
                                with ui.column().classes("gap-0"):
                                    ui.label(translate(item["category"])).classes("font-medium")
                                    ui.label(f'{money(item["previous"])} → {money(item["current"])}').classes("muted text-xs")
                                direction = "increase" if item["change"] > 0 else "decrease"
                                ui.label(f'{"+" if item["change"] > 0 else "−"}{money(abs(item["change"]))}').classes(f"mover-value {direction}")
                    else:
                        ui.label("A second month of data will reveal the largest shifts.").classes("empty compact")

    overview()


@ui.page("/insights")
def insights_page() -> None:
    if not require_unlocked():
        return
    add_global_styles()
    repo = session_repository()
    state = {
        "month": selected_month(),
        "category": app.storage.user.get("insights_category") or "Shopping",
    }

    def refresh() -> None:
        insights.refresh()

    def update(name: str, value: str) -> None:
        if name == "month":
            remember_month(value)
        elif name == "category":
            app.storage.user["insights_category"] = value
        state[name] = value
        refresh()

    editor = ExpenseEditor(repo, refresh)
    header(repo, "insights", editor.open, refresh)
    install_shortcuts(editor)

    @ui.refreshable
    def insights() -> None:
        with repo.read_session() as reader:
            category_order = [
                item["name"] for item in reader.category_library()
                if item["is_active"] or item["transaction_count"]
            ]
            if state["category"] not in category_order:
                state["category"] = category_order[0]
                app.storage.user["insights_category"] = state["category"]
            selected_detail = reader.category_detail(state["month"], state["category"])
            subcategories = reader.subcategory_comparison(
                state["month"], state["category"], count=12,
            )
            selected_budget = reader.budget_trend(
                state["month"], count=12, category=state["category"],
            )
            transaction_insights = reader.transaction_insights(
                state["month"], state["category"], count=12,
            )
            debt = reader.liability_insights(state["month"], count=12)
        subcategory_trends = {
            "months": subcategories["months"],
            "series": [
                {"name": item["subcategory"], "values": item["months"]}
                for item in subcategories["subcategories"]
            ],
        }
        with ui.column().classes("shell gap-5 pb-12"):
            with ui.row().classes("page-head items-end justify-between w-full page-head-row"):
                with ui.column().classes("gap-2"):
                    ui.label(translate("Insights")).classes("title")
                    ui.label(
                        translate(
                            "Patterns, changes, and exceptions derived from your recorded transactions."
                        )
                    ).classes("muted text-sm")
                month_control(state["month"], lambda value: update("month", value))

            if transaction_insights["signals"]:
                with ui.row().classes("section-heading items-end justify-between w-full gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.label(translate("Worth a look")).classes("section-kicker")
                        ui.label(translate(
                            "Notable in {month}", month=month_label(state["month"]),
                        )).classes("subpage-title")
                        ui.label(
                            translate(
                                "Rule-based observations appear only when there is enough history and a material change."
                            )
                        ).classes("muted text-sm")
                with ui.element("section").classes("insight-signal-grid w-full"):
                    for signal in transaction_insights["signals"]:
                        eyebrow, icon, title, detail, metric = insight_signal_copy(signal)
                        with ui.element("article").classes(
                            f'insight-signal-card {signal["kind"]}'
                        ):
                            with ui.row().classes("items-center justify-between w-full no-wrap"):
                                with ui.row().classes("items-center gap-2 no-wrap"):
                                    ui.icon(icon).classes("insight-signal-icon")
                                    ui.label(eyebrow).classes("insight-signal-eyebrow")
                                ui.label(metric).classes("insight-signal-metric")
                            ui.label(title).classes("insight-signal-title")
                            ui.label(detail).classes("insight-signal-detail")

            with ui.element("section").classes("panel p-5 w-full"):
                chart_title(
                    "Purchase frequency",
                    translate(
                        "Number of recorded purchases in each category during {month}. Settlements are tracked separately below.",
                        month=month_label(state["month"]),
                    ),
                )
                if transaction_insights["category_counts"]:
                    transaction_count_chart(transaction_insights["category_counts"])
                else:
                    ui.label(translate("No purchases were recorded for this month.")).classes(
                        "empty compact"
                    )

            with ui.element("section").classes("panel category-focus-panel w-full"):
                with ui.row().classes("category-focus-header items-center justify-between w-full gap-4"):
                    with ui.column().classes("gap-1 min-w-0"):
                        ui.label(translate("Selected category")).classes("eyebrow")
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.element("span").classes("category-focus-dot")
                            ui.label(translate(state["category"])).classes("category-focus-title")
                        ui.label(
                            translate(
                                "Net {amount} in {month} · Purchases {purchases} · Detail groups {groups}",
                                amount=money(transaction_insights["selected"]["net_total_cents"]),
                                month=month_label(state["month"]),
                                purchases=transaction_insights["selected"]["purchase_count"],
                                groups=len(selected_detail["breakdown"]),
                            )
                        ).classes("category-focus-stats")
                    ui.label(translate(
                        "Controls every chart below until Settlements"
                    )).classes("category-focus-scope")
                ui.separator().classes("category-focus-divider")
                ui.label(translate("Change selected category")).classes("field-label mb-0")
                with ui.row().classes("chip-grid"):
                    for category_name in category_order:
                        chip_button(
                            category_name, category_name == state["category"],
                            lambda _, value=category_name: update("category", value),
                        )

            selected = transaction_insights["selected"]
            with ui.element("section").classes("panel insight-stat-grid w-full"):
                for label, value, note in (
                    ("Net spending", money(selected["net_total_cents"]), "Purchases less settlements"),
                    ("Purchases", str(selected["purchase_count"]), "Recorded occasions this month"),
                    ("Average purchase", money(selected["average_purchase_cents"]), "Expenses only"),
                    ("Active days", str(selected["active_days"]), "Distinct purchase dates"),
                ):
                    with ui.column().classes("insight-stat-cell gap-1"):
                        ui.label(translate(label)).classes("muted text-xs")
                        ui.label(value).classes("metric")
                        ui.label(translate(note)).classes("muted text-xs")

            with ui.element("section").classes("chart-grid w-full"):
                with ui.column().classes("panel p-5 gap-1"):
                    chart_title(
                        translate(
                            "{category} activity over time",
                            category=translate(state["category"]),
                        ),
                        "Raw purchase count and net recorded spending by month; annual allocations are not repeated as transactions.",
                    )
                    if any(transaction_insights["activity"]["counts"]):
                        category_activity_chart(transaction_insights["activity"])
                    else:
                        ui.label(translate(
                            "No purchase history is available for this category."
                        )).classes("empty compact")
                with ui.column().classes("panel p-5 gap-1"):
                    chart_title(
                        "Day-of-week pattern",
                        translate(
                            "Purchase timing across the visible twelve months — {category}",
                            category=translate(state["category"]),
                        ),
                    )
                    if any(transaction_insights["weekday"]["counts"]):
                        weekday_activity_chart(transaction_insights["weekday"])
                    else:
                        ui.label(translate(
                            "No dated purchases are available for this category."
                        )).classes("empty compact")

            with ui.element("section").classes("panel p-5 w-full"):
                with ui.row().classes("items-start justify-between w-full gap-3"):
                    chart_title(
                        translate(
                            "{category} detail this month",
                            category=translate(state["category"]),
                        ),
                        "Subcategories when provided; otherwise the recorded vendor. Each transaction appears once.",
                    )
                    ui.label(translate(state["category"])).classes("insight-badge selected-category-badge")
                if selected_detail["breakdown"]:
                    ranked_total_bar(selected_detail["breakdown"], "label")
                else:
                    ui.label(translate(
                        "No spending in this category for this month."
                    )).classes("empty compact")

            with ui.element("section").classes("panel p-5 w-full"):
                with ui.row().classes("items-start justify-between w-full"):
                    trend_support = translate(
                        "Five largest subcategory-first contributors; remaining detail is grouped exactly."
                    )
                    if any(value and value > 0 for value in selected_budget["values"]):
                        trend_support += " " + translate(
                            "The dashed red line is the effective category budget."
                        )
                    chart_title(
                        f'{translate(state["category"])} · {translate("Category detail")}',
                        trend_support,
                    )
                    ui.link(
                        translate("Open expenses"),
                        f'/expenses?month={state["month"]}&category={quote(state["category"])}',
                    ).classes("text-link")
                if subcategory_trends["series"]:
                    stacked_area_chart(
                        subcategory_trends, height=340, other_label="Other details",
                        budget_values=selected_budget["values"],
                    )
                else:
                    ui.label(translate(
                        "No subcategory data for this category yet."
                    )).classes("empty compact")

            with ui.element("section").classes("panel p-5 w-full comparison-table-panel"):
                with ui.row().classes("items-center justify-between w-full mb-4"):
                    chart_title(
                        f'{translate(state["category"])} · {translate("Category detail")}',
                        "Exact subcategory values, with vendor fallback, across the same twelve months.",
                    )
                    ui.link(
                        translate("Open expenses"),
                        f'/expenses?month={state["month"]}&category={quote(state["category"])}',
                    ).classes("text-link")
                if subcategories["subcategories"]:
                    with ui.element("div").classes("comparison-table-scroll"):
                        with ui.element("div").classes("comparison-table twelve-month"):
                            ui.label(translate("Subcategory")).classes(
                                "comparison-cell comparison-header sticky-column"
                            )
                            for month_value in subcategories["months"]:
                                ui.label(month_label(month_value, short=True)).classes("comparison-cell comparison-header amount")
                            ui.label(translate("12-month total")).classes(
                                "comparison-cell comparison-header amount"
                            )
                            for row in subcategories["subcategories"]:
                                ui.label(row["subcategory"]).classes(
                                    "comparison-cell font-medium sticky-column"
                                )
                                for value in row["months"]:
                                    ui.label(money(value) if value else "—").classes("comparison-cell amount")
                                ui.label(money(row["total"])).classes("comparison-cell amount font-medium")
                else:
                    ui.label(translate(
                        "No subcategory data for this category yet."
                    )).classes("empty compact")

            if any(transaction_insights["settlements"]["counts"]):
                with ui.row().classes("section-heading items-end justify-between w-full gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.label(translate("Reimbursements")).classes("section-kicker")
                        ui.label(translate("Settlement activity")).classes("subpage-title")
                        ui.label(
                            translate(
                                "Signed settlements reduce their assigned category and cumulative outgoing cash."
                            )
                        ).classes("muted text-sm")
                with ui.element("section").classes("chart-grid w-full"):
                    with ui.column().classes("panel p-5 gap-1"):
                        chart_title(
                            "Settled over time",
                            "Amount returned and number of settlement entries by transaction month.",
                        )
                        settlement_activity_chart(transaction_insights["settlements"])
                    with ui.column().classes("panel p-5 gap-2"):
                        chart_title(
                            translate(
                                "{month} settlements", month=month_label(state["month"]),
                            ),
                            "Categories affected in the selected month.",
                        )
                        if transaction_insights["settlements"]["by_category"]:
                            for item in transaction_insights["settlements"]["by_category"]:
                                with ui.row().classes(
                                    "settlement-insight-row items-center justify-between w-full"
                                ):
                                    with ui.column().classes("gap-0"):
                                        ui.label(translate(item["category"])).classes("font-medium")
                                        ui.label(translate(
                                            "{count} settlement entries", count=item["count"],
                                        )).classes("muted text-xs")
                                    ui.label(money(item["total"])).classes("metric text-base")
                        else:
                            ui.label(translate(
                                "No settlements in the selected month; the chart retains earlier activity."
                            )).classes("empty compact")

            if debt["loans"]:
                payoff_months = debt["projected_payoff_months"]
                payoff_summary = (
                    "Payoff cannot be projected at the current payment"
                    if payoff_months is None else
                    f'Projected debt-free in {payoff_months // 12}y {payoff_months % 12}m'
                )
                with ui.row().classes("section-heading items-end justify-between w-full gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.label(translate("Loans & liabilities")).classes("section-kicker")
                        ui.label("Debt repayment").classes("subpage-title")
                        ui.label(
                            f'{payoff_summary} · as of {date_label(debt["as_of"])}'
                        ).classes("muted text-sm")
                    ui.link(translate("Manage loans"), "/liabilities").classes("text-link")

                with ui.element("section").classes("panel debt-summary w-full"):
                    for label, value, note in (
                        ("Remaining balance", money(debt["total_balance_cents"]), "Across active liabilities"),
                        ("Principal repaid", money(debt["total_repaid_cents"]), "Since each loan started"),
                        ("Principal reduction / month", money(debt["paydown_pace_cents"]), "Average across the visible window"),
                        ("Debt payments / month", money(debt["monthly_payment_cents"]), "Observed average or contract"),
                    ):
                        with ui.column().classes("summary-cell gap-1"):
                            ui.label(translate(label)).classes("muted text-xs")
                            ui.label(value).classes("metric")
                            ui.label(translate(note)).classes("muted text-xs")

                with ui.element("section").classes("chart-grid w-full"):
                    with ui.column().classes("panel p-5 gap-1"):
                        chart_title(
                            "Balance trend",
                            "Remaining principal at each month end, using matched payments when available.",
                        )
                        liability_balance_chart(debt)
                    with ui.column().classes("panel p-5 gap-1"):
                        chart_title(
                            "Payment trend",
                            "Imported matches are observed. Missing matches use the contractual payment as a clearly separated fallback.",
                        )
                        liability_payment_chart(debt)

                with ui.element("section").classes("loan-insights-grid w-full"):
                    for loan in debt["loans"]:
                        loan_payoff = loan["projected_payoff_months"]
                        payoff_label = (
                            "Not amortizing" if loan_payoff is None else
                            f'{loan_payoff // 12}y {loan_payoff % 12}m'
                        )
                        payment_label = (
                            "Observed from imports" if loan["payment_source"] == "observed"
                            else "Contractual payment"
                        )
                        with ui.column().classes("panel loan-insight-card gap-3"):
                            with ui.row().classes("items-start justify-between w-full gap-3"):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(loan["name"]).classes("section-title")
                                    ui.label(
                                        f'{loan["liability_type"]} · {loan["rate_type"]}'
                                    ).classes("muted text-xs")
                                ui.label(payment_label).classes(
                                    f'insight-badge {"observed" if loan["payment_source"] == "observed" else "fallback"}'
                                )
                            with ui.row().classes("items-end justify-between w-full gap-3"):
                                with ui.column().classes("gap-0"):
                                    ui.label(money(loan["balance_cents"])).classes("loan-insight-balance")
                                    ui.label("remaining principal").classes("muted text-xs")
                                ui.label(f'{loan["repaid_percent"]:.1f}% repaid').classes("loan-progress-label")
                            ui.linear_progress(
                                value=min(max(loan["repaid_percent"] / 100, 0), 1),
                                show_value=False,
                            ).props("rounded color=primary")
                            with ui.element("div").classes("loan-insight-metrics"):
                                for label, value in (
                                    ("Rate", f'{loan["annual_rate_bps"] / 100:.2f}%'),
                                    ("Monthly payment", money(loan["projected_payment_cents"])),
                                    ("Paydown pace", f'{money(loan["paydown_pace_cents"])} / mo'),
                                    ("Projected payoff", payoff_label),
                                ):
                                    with ui.column().classes("gap-0"):
                                        ui.label(translate(label)).classes("muted text-xs")
                                        ui.label(value).classes("font-medium text-sm")
                            if loan["observed_months"]:
                                ui.label(
                                    f'{loan["observed_months"]} month(s) matched from imported transactions'
                                ).classes("muted text-xs")
    insights()


@ui.page("/budget")
def budget_page() -> None:
    if not require_unlocked():
        return
    add_global_styles()
    repo = session_repository()
    state = {"month": selected_month()}

    def refresh() -> None:
        content.refresh()

    def set_month(value: str) -> None:
        remember_month(value)
        state["month"] = value
        refresh()

    editor = ExpenseEditor(repo, refresh)
    header(repo, "budget", editor.open, refresh)
    install_shortcuts(editor)

    @ui.refreshable
    def content() -> None:
        with repo.read_session() as reader:
            rows = reader.budgets(state["month"])
            plan = reader.budget_plan_info(state["month"])
            total_limit = reader.total_monthly_budget()
        inputs: dict[str, object] = {}
        scope = {"value": "from_month"}
        budgeted_categories = sum(row["amount_cents"] > 0 for row in rows)
        total_actual = sum(row["actual_cents"] for row in rows)

        def save() -> None:
            try:
                repo.save_budgets(
                    state["month"], {category: field.value for category, field in inputs.items()},
                    scope["value"],
                    total_amount=total_limit_input.value,
                )
            except AuthorizationExpired:
                raise
            except Exception as error:
                ui.notify(f"Could not save budget: {error}", color="negative", position="bottom")
                return
            ui.notify("Budget saved", color="positive", position="bottom")
            refresh()

        with ui.column().classes("shell gap-5 pb-12"):
            with ui.row().classes("page-head items-end justify-between w-full page-head-row"):
                with ui.column().classes("gap-2"):
                    ui.label(translate("Budget")).classes("title")
                    ui.label("Set category ceilings that stay in effect until you revise them. Annual expenses use their monthly equivalent.").classes("muted text-sm")
                month_control(state["month"], set_month)

            with ui.element("section").classes("panel budget-summary w-full"):
                with ui.column().classes("summary-cell gap-1"):
                    ui.label(translate("Total monthly limit")).classes("muted text-xs")
                    total_limit_input = ui.input(
                        value=f'{total_limit / 100:.2f}' if total_limit else "",
                        placeholder=translate("Optional"),
                    ).props("borderless dense prefix='$' inputmode=decimal").classes(
                        "budget-total-input"
                    )
                    ui.label(translate("Used only for the total spending trend.")).classes(
                        "muted text-xs"
                    )
                for label, value, display in (
                    (
                        "Categories budgeted", budgeted_categories,
                        f"{budgeted_categories} / {len(rows)}",
                    ),
                    ("Monthly cost", total_actual, money(total_actual)),
                    (
                        "Remaining",
                        total_limit - total_actual if total_limit is not None else None,
                        money(total_limit - total_actual) if total_limit is not None else translate("Not set"),
                    ),
                ):
                    with ui.column().classes("summary-cell gap-1"):
                        ui.label(translate(label)).classes("muted text-xs")
                        ui.label(display).classes(
                            f"metric {'negative' if label == 'Remaining' and value is not None and value < 0 else ''}"
                        )
                with ui.row().classes("items-center justify-end gap-2 budget-summary-actions"):
                    primary_action("Save budget", "save", save)

            with ui.element("section").classes("panel budget-scope-panel w-full"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label("Budget in effect").classes("field-label mb-0")
                    if not plan["has_budget"]:
                        ui.label("Your first saved budget becomes the default for all months.").classes("muted text-sm")
                    else:
                        effective = (
                            "Default budget"
                            if plan["is_default"] else f'Revised {month_label(plan["effective_month"])}'
                        )
                        ui.label(f'{effective} · choose how this revision should apply').classes("muted text-sm")
                if plan["has_budget"]:
                    scope_select = ui.select(
                        {
                            "from_month": "From this month onward",
                            "all_time": "All months (including previous)",
                            "year": f"All months in {state['month'][:4]} only",
                        },
                        value="from_month",
                        on_change=lambda event: scope.update(value=event.value),
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("budget-scope-select")

            with ui.element("section").classes("panel budget-table w-full"):
                with ui.element("div").classes("budget-row budget-header"):
                    ui.label(translate("Category"))
                    ui.label("Budget")
                    ui.label("Monthly cost")
                    ui.label("Status")
                for row in rows:
                    with ui.element("div").classes("budget-row"):
                        ui.label(translate(row["category"])).classes("font-medium")
                        field = ui.input(
                            value=f'{row["amount_cents"] / 100:.2f}' if row["amount_cents"] else "",
                            placeholder="0.00",
                        ).props("outlined dense prefix='$' inputmode=decimal").classes("budget-input")
                        inputs[row["category"]] = field
                        ui.label(money(row["actual_cents"])).classes("amount")
                        with ui.column().classes("gap-1 min-w-0"):
                            ratio = row["actual_cents"] / row["amount_cents"] if row["amount_cents"] else 0
                            ui.linear_progress(
                                value=min(max(ratio, 0), 1), show_value=False,
                            ).props(
                                f"rounded color={'negative' if ratio > 1 else 'primary'}"
                            ).classes("budget-progress")
                            if row["amount_cents"]:
                                message = (
                                    f'{money(abs(row["remaining_cents"]))} over'
                                    if row["remaining_cents"] < 0
                                    else f'{money(row["remaining_cents"])} left'
                                )
                            else:
                                message = "No budget set"
                            ui.label(message).classes(
                                f"text-xs {'negative' if row['remaining_cents'] < 0 and row['amount_cents'] else 'muted'}"
                            )
    content()


@ui.page("/accounts")
def accounts_page() -> None:
    if not require_unlocked():
        return
    add_global_styles()
    repo = session_repository()

    def refresh() -> None:
        content.refresh()

    expense_editor = ExpenseEditor(repo, refresh)
    account_editor = AccountEditor(repo, refresh)
    header(repo, "accounts", expense_editor.open, refresh)
    install_shortcuts(expense_editor)

    def toggle_active(record: dict) -> None:
        active = not bool(record["is_active"])
        try:
            repo.set_account_active(record["id"], active)
        except AuthorizationExpired:
            raise
        except Exception:
            ui.notify(
                "The account could not be updated. Your data was not changed.",
                color="negative", position="bottom",
            )
            return
        if not active and ENTRY_DEFAULTS.get("account_id") == record["id"]:
            ENTRY_DEFAULTS["account_id"] = 0
        refresh()

    @ui.refreshable
    def content() -> None:
        accounts = repo.accounts(include_inactive=True)
        with ui.column().classes("shell gap-5 pb-12"):
            with ui.row().classes("page-head items-end justify-between w-full page-head-row"):
                with ui.column().classes("gap-2"):
                    ui.label(translate("Accounts")).classes("title")
                    ui.label(
                        translate(
                            "Optional labels for where transactions occurred—without changing the unified spending view."
                        )
                    ).classes("muted text-sm")
                primary_action("Add account", "add_card", lambda: account_editor.open())

            with ui.element("section").classes("panel account-scope-note w-full"):
                ui.icon("info_outline").classes("muted text-lg")
                with ui.column().classes("gap-0"):
                    ui.label(translate("Accounts are optional")).classes("font-medium text-sm")
                    ui.label(
                        translate(
                            "Unassigned expenses and imports continue to work normally. Archiving an account never removes its transactions."
                        )
                    ).classes("muted text-xs")

            if not accounts:
                with ui.element("section").classes("panel w-full"):
                    ui.label(translate("No accounts configured.")).classes("empty compact")
                    ui.label(
                        translate(
                            "Add a bank account or card to make future entry and imports easier."
                        )
                    ).classes("muted text-sm text-center pb-8")
            else:
                with ui.element("section").classes("account-grid w-full"):
                    for record in accounts:
                        with ui.column().classes(
                            f'panel account-card gap-3 {"archived" if not record["is_active"] else ""}'
                        ):
                            with ui.row().classes("items-start justify-between w-full gap-3"):
                                with ui.row().classes("items-center gap-3 min-w-0"):
                                    ui.icon(
                                        "credit_card" if record["account_type"] == "Credit card"
                                        else "account_balance"
                                    ).classes("account-card-icon")
                                    with ui.column().classes("gap-0 min-w-0"):
                                        ui.label(account_label(record)).classes(
                                            "section-title ellipsis"
                                        )
                                        details = record["account_type"]
                                        if record["institution"]:
                                            details += f' · {record["institution"]}'
                                        ui.label(details).classes("muted text-xs")
                                with ui.row().classes("gap-0 no-wrap"):
                                    if not record["is_active"]:
                                        ui.label(translate("Archived")).classes(
                                            "insight-badge fallback account-status"
                                        )
                                    ui.button(
                                        icon="edit_outlined",
                                        on_click=lambda _, row=record: account_editor.open(row),
                                    ).props(
                                        f"flat round dense color=grey-6 aria-label='{translate('Edit account')}' "
                                        f"title='{translate('Edit account')}'"
                                    )
                                    account_action = translate(
                                        "Restore account" if not record["is_active"] else "Archive account"
                                    )
                                    ui.button(
                                        icon="unarchive" if not record["is_active"] else "archive",
                                        on_click=lambda _, row=record: toggle_active(row),
                                    ).props(
                                        "flat round dense color=grey-6 "
                                        f"aria-label='{account_action}' title='{account_action}'"
                                    )
                            with ui.row().classes("account-card-meta items-center gap-4"):
                                ui.label(
                                    translate(
                                        "{count} linked transaction"
                                        if record["transaction_count"] == 1
                                        else "{count} linked transactions",
                                        count=record["transaction_count"],
                                    )
                                ).classes("muted text-xs")
                                if record["last_used"]:
                                    ui.label(
                                        translate(
                                            "Last used {date}",
                                            date=date_label(record["last_used"], include_year=True),
                                        )
                                    ).classes("muted text-xs")
    content()


@ui.page("/liabilities")
def liabilities_page() -> None:
    if not require_unlocked():
        return
    add_global_styles()
    repo = session_repository()

    def refresh() -> None:
        content.refresh()

    expense_editor = ExpenseEditor(repo, refresh)
    loan_editor = LiabilityEditor(repo, refresh)
    header(repo, "liabilities", expense_editor.open, refresh)
    install_shortcuts(expense_editor)

    async def remove(record: dict) -> None:
        with ui.dialog() as dialog, ui.card().classes("panel p-5 delete-dialog"):
            ui.label("Delete this loan?").classes("section-title")
            ui.label(record["name"]).classes("muted")
            ui.label("This removes the loan record only. It does not change expenses or net worth.").classes("text-sm")
            with ui.row().classes("justify-end w-full mt-3"):
                ui.button(translate("Cancel"), on_click=dialog.close).props("flat no-caps")
                ui.button(translate("Delete"), on_click=lambda: dialog.submit(True)).props("unelevated no-caps color=negative")
        if await dialog:
            repo.delete_liability(record["id"])
            refresh()

    @ui.refreshable
    def content() -> None:
        loans = repo.liabilities()
        total_balance = sum(
            item["estimated_balance_cents"] if item["observed_months"] else item["scheduled_balance_cents"]
            for item in loans
        )
        total_payments = sum(
            item["projected_payment_cents"] for item in loans
            if item["scheduled_balance_cents"] > 0
        )
        with ui.column().classes("shell gap-5 pb-12"):
            with ui.row().classes("page-head items-end justify-between w-full page-head-row"):
                with ui.column().classes("gap-2"):
                    ui.label(translate("Loans & liabilities")).classes("title")
                    ui.label("Calculate payments and project the balance from current lender figures.").classes("muted text-sm")
                primary_action("Add loan", "add", lambda: loan_editor.open())
            with ui.element("section").classes("panel liability-summary w-full"):
                for label, value in (("Estimated balance", total_balance), ("Monthly payment total", total_payments), ("Active loans", len([item for item in loans if item["scheduled_balance_cents"] > 0]))):
                    with ui.column().classes("summary-cell gap-1"):
                        ui.label(label).classes("muted text-xs")
                        ui.label(str(value) if label == "Active loans" else money(value)).classes("metric")
            if not loans:
                with ui.column().classes("panel loan-empty-state items-center gap-1 w-full"):
                    ui.label("No loans yet.").classes("font-medium muted")
                    ui.label(
                        "Add current lender figures to calculate a payment and projected balance."
                    ).classes("muted text-sm text-center")
            else:
                with ui.element("section").classes("loan-grid w-full"):
                    for record in loans:
                        shown_balance = (
                            record["estimated_balance_cents"]
                            if record["observed_months"] else record["scheduled_balance_cents"]
                        )
                        progress = 1 - shown_balance / record["original_principal_cents"]
                        with ui.column().classes("panel loan-card gap-3"):
                            with ui.row().classes("items-start justify-between w-full"):
                                with ui.column().classes("gap-0"):
                                    ui.label(record["name"]).classes("section-title")
                                    ui.label(
                                        f'{record["liability_type"]} · {record["rate_type"]} · '
                                        f'{record["interest_convention"]}'
                                    ).classes("muted text-xs")
                                with ui.row().classes("gap-0"):
                                    ui.button(icon="edit_outlined", on_click=lambda _, row=record: loan_editor.open(row)).props(
                                        "flat round dense color=grey-6 aria-label='Edit loan' title='Edit loan'"
                                    )
                                    ui.button(icon="delete_outline", on_click=lambda _, row=record: remove(row)).props(
                                        "flat round dense color=grey-6 aria-label='Delete loan' title='Delete loan'"
                                    )
                            ui.label(money(shown_balance)).classes("loan-balance")
                            ui.label(
                                "Estimated forward from matched payments" if record["observed_months"]
                                else f'Projected from {record["balance_as_of_date"]} balance'
                            ).classes("muted text-xs")
                            ui.linear_progress(
                                value=min(max(progress, 0), 1), show_value=False,
                            ).props("rounded color=primary")
                            with ui.element("div").classes("loan-metrics"):
                                with ui.column().classes("gap-0"):
                                    ui.label(record["payment_frequency"]).classes("muted text-xs")
                                    ui.label(money(record["payment_cents"])).classes("font-medium")
                                with ui.column().classes("gap-0"):
                                    ui.label("Rate").classes("muted text-xs")
                                    ui.label(f'{record["annual_rate_bps"] / 100:.2f}%').classes("font-medium")
                                with ui.column().classes("gap-0"):
                                    ui.label("Remaining amortization").classes("muted text-xs")
                                    ui.label(
                                        f'{record["term_months"] // 12}y · '
                                        f'{record["rate_term_months"] // 12}y rate term'
                                    ).classes("font-medium")
                            if record["payment_match_key"]:
                                with ui.element("div").classes("loan-observed-panel"):
                                    with ui.row().classes("items-center justify-between w-full"):
                                        with ui.column().classes("gap-0"):
                                            ui.label("Matched transaction").classes("muted text-xs")
                                            ui.label(record["payment_match_label"]).classes("font-medium text-sm")
                                        ui.label(f'{record["observed_months"]} observed month(s)').classes("insight-badge")
                                    if record["observed_payment_cents"]:
                                        months = record["projected_payoff_months"]
                                        payoff = (
                                            "Payment does not amortize the balance"
                                            if months is None else
                                            f'{months // 12}y {months % 12}m projected payoff'
                                        )
                                        ui.label(
                                            f'Recent weighted payment {money(record["observed_payment_cents"])} · {payoff}'
                                        ).classes("text-sm")
                                    else:
                                        ui.label("No matching imported payments yet.").classes("muted text-sm")
                            ui.label(
                                f'Projection starts from the recorded {record["balance_as_of_date"]} balance. '
                                "It treats the entered rate as a forward scenario; renewals, future rate changes, fees, escrow, and insurance are not inferred."
                            ).classes("estimate-note")
    content()


@ui.page("/expenses")
def expenses_page(month: str | None = None, category: str | None = None) -> None:
    if not require_unlocked():
        return
    add_global_styles()
    repo = session_repository()
    known_categories = {item["name"] for item in repo.category_settings()}
    filters = {
        "month": selected_month(month),
        "search": "",
        "category": category if category in known_categories else "All",
        "expense_type": "All",
        "view": "grouped",
    }

    def refresh() -> None:
        listing.refresh()

    def update(name: str, value: str) -> None:
        if name == "month":
            remember_month(value)
        filters[name] = value
        refresh()

    editor = ExpenseEditor(repo, refresh)
    bank_import = BankImportDialog(repo, refresh)
    header(repo, "expenses", editor.open, refresh)
    install_shortcuts(editor)

    async def confirm_delete(record: dict) -> None:
        with ui.dialog() as dialog, ui.card().classes("panel p-5 delete-dialog"):
            noun = "settlement" if record["transaction_kind"] == SETTLEMENT_KIND else "expense"
            ui.label(translate("Delete this settlement?" if noun == "settlement" else "Delete this expense?")).classes("section-title")
            ui.label(f'{record["description"]} · {money(record["amount_cents"])}').classes("muted")
            ui.label("This cannot be undone.").classes("text-sm")
            with ui.row().classes("justify-end w-full mt-3"):
                ui.button(translate("Cancel"), on_click=dialog.close).props("flat no-caps color=grey-8")
                ui.button(translate("Delete"), on_click=lambda: dialog.submit(True)).props(
                    "unelevated no-caps color=negative"
                )
        if await dialog:
            try:
                repo.delete(record["id"])
            except AuthorizationExpired:
                raise
            except Exception:
                ui.notify(
                    "The expense could not be deleted. Your data was not changed.",
                    color="negative", position="bottom", timeout=3000,
                )
                return
            refresh()
            ui.notify("Expense deleted", position="bottom", timeout=1500)

    def transaction_row(record: dict) -> None:
        with ui.element("div").classes("transaction").on("click", lambda: editor.open(record)):
            ui.label(date_label(record["date"])).classes("muted text-xs")
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(record["description"]).classes("font-medium ellipsis")
                details = []
                if record.get("subcategory"):
                    details.append(record["subcategory"])
                if record.get("account_name"):
                    account_detail = record["account_name"]
                    if record.get("account_last_four"):
                        account_detail += f' •••• {record["account_last_four"]}'
                    details.append(account_detail)
                if record["transaction_kind"] == SETTLEMENT_KIND:
                    details.append(translate("Settlement"))
                if record["expense_type"] == ANNUAL_EXPENSE_TYPE:
                    details.append(translate("Annual"))
                if details:
                    ui.label(" · ".join(details)).classes("subcategory-label")
            ui.label(translate(record["category"])).classes("category muted text-sm")
            with ui.row().classes("items-center justify-end gap-1"):
                amount_classes = (
                    "amount settlement-amount"
                    if record["transaction_kind"] == SETTLEMENT_KIND else "amount"
                )
                ui.label(money(record["amount_cents"])).classes(amount_classes)
                ui.button(icon="edit_outlined").props(
                    "flat round dense color=grey-6 aria-label='Edit expense' title='Edit expense'"
                ).on("click.stop", lambda _, row=record: editor.open(row))
                ui.button(icon="delete_outline").props(
                    "flat round dense color=grey-6 aria-label='Delete expense' title='Delete expense'"
                ).on("click.stop", lambda _, row=record: confirm_delete(row))

    def recent_imports_panel(imports: list[dict]) -> None:
        with ui.element("section").classes("panel recent-imports-panel"):
            with ui.row().classes("recent-imports-heading items-start no-wrap"):
                ui.icon("history").classes("recent-imports-icon")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(translate("Recent imports")).classes("section-title")
                    ui.label(translate(
                        "Reviewed imports are listed here. Original CSV files are never stored."
                    )).classes("muted text-xs")
            if not imports:
                ui.label(translate(
                    "Your next reviewed import will appear here."
                )).classes("recent-imports-empty")
                return
            with ui.column().classes("recent-imports-list gap-0"):
                for record in imports:
                    with ui.element("div").classes("recent-import-row"):
                        with ui.column().classes("recent-import-main gap-1 min-w-0"):
                            ui.label(record["filename"]).classes("recent-import-filename ellipsis")
                            with ui.row().classes("recent-import-context items-center"):
                                ui.label(record["bank"]).classes("recent-import-bank")
                                if record.get("account_name"):
                                    ui.label(account_label({
                                        "name": record["account_name"],
                                        "last_four": record.get("account_last_four", ""),
                                    })).classes("recent-import-account")
                                start = date_label(
                                    record["first_transaction_date"], include_year=True,
                                )
                                end = date_label(
                                    record["last_transaction_date"], include_year=True,
                                )
                                date_range = start if start == end else f"{start} – {end}"
                                ui.label(date_range).classes("recent-import-range")
                        with ui.column().classes("recent-import-stats gap-0"):
                            ui.label(translate(
                                "Imported {imported} of {selected} selected",
                                imported=record["imported_count"],
                                selected=record["selected_row_count"],
                            )).classes("text-xs")
                            ui.label(
                                f'{translate("{count} source rows", count=record["source_row_count"])} · '
                                f'{translate("Imported {date}", date=date_label(record["created_at"], include_year=True))}'
                            ).classes("muted text-xs")

    @ui.refreshable
    def listing() -> None:
        sort = "category" if filters["view"] == "grouped" else filters["view"]
        unfiltered = (
            not filters["search"].strip()
            and filters["category"] == "All"
            and filters["expense_type"] == "All"
        )
        with repo.read_session() as reader:
            records = reader.list(
                filters["month"], filters["search"], filters["category"],
                filters["expense_type"], sort=sort,
            )
            all_records = records if unfiltered else reader.list(filters["month"])
            category_order = [item["name"] for item in reader.category_settings()]
            recent_imports = reader.recent_bank_imports()
        category_counts = defaultdict(int)
        for record in all_records:
            category_counts[record["category"]] += 1
        with ui.column().classes("shell gap-5 pb-12"):
            with ui.row().classes("page-head items-end justify-between w-full page-head-row"):
                with ui.column().classes("gap-2"):
                    ui.label(translate("Expenses")).classes("title")
                    ui.label(
                        f'{entry_count(len(records))} · {money(sum(row["amount_cents"] for row in records))}'
                    ).classes("muted text-sm")
                with ui.row().classes("items-center gap-2"):
                    primary_action("Import CSV", "upload_file", bank_import.open)
                    month_control(filters["month"], lambda value: update("month", value))

            recent_imports_panel(recent_imports)

            with ui.element("section").classes("panel filter-panel"):
                with ui.row().classes("filter-top w-full items-center"):
                    ui.input(
                        value=filters["search"], placeholder=translate("Search expenses"),
                        on_change=lambda event: update("search", event.value),
                    ).props("outlined dense clearable debounce=250").classes("search-input")
                    ui.toggle(
                        {"grouped": translate("Grouped"), "date": translate("Newest"), "amount": translate("Largest")},
                        value=filters["view"], on_change=lambda event: update("view", event.value),
                    ).props("no-caps dense unelevated").classes("view-toggle")
                with ui.row().classes("chip-grid"):
                    chip_button(
                        "All", filters["category"] == "All",
                        lambda: update("category", "All"), len(all_records),
                    )
                    for category_name in category_order:
                        count = category_counts.get(category_name, 0)
                        if count or filters["category"] == category_name:
                            chip_button(
                                category_name, filters["category"] == category_name,
                                lambda _, value=category_name: update("category", value), count,
                            )
                with ui.row().classes("chip-grid type-filter"):
                    for expense_type, label in (
                        ("All", "All"), ("Regular", "Regular"),
                        (ANNUAL_EXPENSE_TYPE, "Annual"),
                    ):
                        chip_button(
                            label, filters["expense_type"] == expense_type,
                            lambda _, value=expense_type: update("expense_type", value),
                        )

            if not records:
                with ui.element("section").classes("panel w-full"):
                    ui.label(translate("No matching expenses.")).classes("empty")
            elif filters["view"] == "grouped":
                groups: dict[str, list[dict]] = defaultdict(list)
                for record in records:
                    groups[record["category"]].append(record)
                for category_name in category_order:
                    group = groups.get(category_name)
                    if not group:
                        continue
                    with ui.element("section").classes("panel category-group w-full"):
                        with ui.row().classes("category-group-head items-center justify-between w-full"):
                            with ui.row().classes("items-baseline gap-2"):
                                ui.label(translate(category_name)).classes("section-title")
                                ui.label(entry_count(len(group))).classes("muted text-xs")
                            ui.label(money(sum(item["amount_cents"] for item in group))).classes("metric text-base")
                        for record in group:
                            transaction_row(record)
            else:
                with ui.element("section").classes("panel w-full"):
                    for record in records:
                        transaction_row(record)
    listing()


def run() -> None:
    configured_port = os.environ.get("EXPENSETICS_PORT")
    port = int(configured_port) if configured_port else (
        native.find_open_port() if getattr(sys, "frozen", False) else 8080
    )
    ui.run(
        title="Expensetics", favicon=ASSET_PATH / "logo.svg", reload=False,
        show=os.environ.get("EXPENSETICS_SHOW_BROWSER", "1") != "0",
        host=local_host(os.environ.get("EXPENSETICS_HOST")), port=port,
        storage_secret=storage_secret(DATA_DIR),
        session_middleware_kwargs={"same_site": "strict"},
    )
