from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from .components import primary_action
from .formatting import date_label, money
from .i18n import translate
from .repository import Repository
from .session_security import AuthorizationExpired


ALL_SUBCATEGORIES = "__all__"


class CategoryMigrationDialog:
    """Explicit, previewed mappings for historical transaction categories."""

    def __init__(self, repository: Repository, on_changed: Callable[[], None]) -> None:
        self.repository = repository
        self.on_changed = on_changed
        self.source_category_id: int | None = None
        self.target_category_id: int | None = None
        self.source_subcategory: str | None = None
        self.target_action = "keep"
        self.target_subcategory = ""
        self.preview: dict | None = None
        self.error_message = ""
        with ui.dialog() as self.help_dialog, ui.card().classes("confirm-card"):
            ui.label(translate("What category migration means")).classes("section-title")
            ui.label(
                translate(
                    "A category migration rewrites matching historical transactions from one category or subcategory to another."
                )
            ).classes("text-sm")
            ui.label(
                translate(
                    "It also updates matching learned bank-import choices. Budgets and category definitions are not changed."
                )
            ).classes("muted text-sm")
            ui.label(
                translate(
                    "You can leave old records unchanged and return later. Every applied mapping is logged, but it is not automatically undoable. Create an encrypted backup first when the change is important."
                )
            ).classes("muted text-sm")
            with ui.row().classes("justify-end w-full"):
                ui.button(translate("Close"), on_click=self.help_dialog.close).props(
                    "flat no-caps"
                )
        self.dialog = ui.dialog()
        with self.dialog:
            self.card = ui.card().classes("category-migration-card")
        self._content_rendered = False

    def open(
        self,
        source_category_id: int | None = None,
        target_category_id: int | None = None,
    ) -> None:
        library = self.repository.category_library()
        if not library:
            return
        identifiers = {category["id"] for category in library}
        if source_category_id not in identifiers:
            source_category_id = next(
                (
                    category["id"] for category in library
                    if not category["is_active"] and category["transaction_count"]
                ),
                library[0]["id"],
            )
        active = [category for category in library if category["is_active"]]
        self.source_category_id = source_category_id
        active_identifiers = {category["id"] for category in active}
        self.target_category_id = (
            target_category_id if target_category_id in active_identifiers else next(
                (
                    category["id"] for category in active
                    if category["id"] != source_category_id
                ),
                active[0]["id"],
            )
        )
        self.source_subcategory = None
        self.target_action = "keep"
        self.target_subcategory = ""
        self.preview = None
        self.error_message = ""
        if self._content_rendered:
            self.content.refresh()
        else:
            with self.card:
                self.content()
            self._content_rendered = True
        self.dialog.open()

    def _changed(self) -> None:
        self.preview = None
        self.error_message = ""

    def _set_source(self, value: int) -> None:
        self.source_category_id = int(value)
        self.source_subcategory = None
        self._changed()
        self.content.refresh()

    def _set_target(self, value: int) -> None:
        self.target_category_id = int(value)
        self.target_subcategory = ""
        self._changed()
        self.content.refresh()

    def _set_source_subcategory(self, value: str) -> None:
        self.source_subcategory = None if value == ALL_SUBCATEGORIES else value
        self._changed()

    def _set_target_action(self, value: str) -> None:
        self.target_action = value
        self.target_subcategory = ""
        self._changed()
        self.content.refresh()

    def _preview(self) -> None:
        try:
            self.preview = self.repository.category_migration_preview(
                int(self.source_category_id), int(self.target_category_id),
                source_subcategory=self.source_subcategory,
                target_subcategory_action=self.target_action,
                target_subcategory=self.target_subcategory,
            )
            if self.preview["no_change"]:
                raise ValueError("Choose a mapping that changes the historical records")
            self.error_message = ""
        except (TypeError, ValueError) as error:
            self.preview = None
            self.error_message = str(error)
        self.content.refresh()

    async def _apply(self) -> None:
        if not self.preview:
            return
        preview = self.preview
        with ui.dialog() as confirmation, ui.card().classes("confirm-card"):
            ui.label(translate("Apply this historical mapping?")).classes("section-title")
            ui.label(
                translate(
                    "{count} matching transactions will be rewritten. This is not automatically undoable.",
                    count=preview["transaction_count"],
                )
            ).classes("text-sm")
            ui.label(
                f'{preview["source_category"]} → {preview["target_category"]}'
            ).classes("category-migration-route")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(translate("Cancel"), on_click=confirmation.close).props(
                    "flat no-caps color=grey-7"
                )
                ui.button(
                    translate("Apply mapping"),
                    on_click=lambda: confirmation.submit(True),
                ).props("unelevated no-caps color=negative")
        if not await confirmation:
            return
        try:
            result = self.repository.apply_category_migration(
                int(self.source_category_id), int(self.target_category_id),
                source_subcategory=self.source_subcategory,
                target_subcategory_action=self.target_action,
                target_subcategory=self.target_subcategory,
            )
        except AuthorizationExpired:
            raise
        except ValueError as error:
            self.error_message = str(error)
            self.preview = None
            self.content.refresh()
            return
        except Exception:
            self.error_message = "The historical mapping could not be applied. No data was changed."
            self.preview = None
            self.content.refresh()
            return
        self.preview = None
        self.error_message = ""
        self.on_changed()
        self.content.refresh()
        ui.notify(
            f'{result["transaction_count"]} historical transaction(s) updated',
            color="positive", position="bottom",
        )

    @ui.refreshable
    def content(self) -> None:
        library = self.repository.category_library()
        categories = {category["id"]: category["name"] for category in library}
        active = {
            category["id"]: category["name"]
            for category in library if category["is_active"]
        }
        source_subcategories = (
            self.repository.historical_subcategories(int(self.source_category_id))
            if self.source_category_id else []
        )
        target_category = next(
            (
                category for category in library
                if category["id"] == self.target_category_id
            ),
            None,
        )
        target_subcategories = [
            subcategory["name"] for subcategory in (target_category or {}).get(
                "subcategories", []
            ) if subcategory["is_active"]
        ]

        with ui.row().classes("editor-head items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(translate("Migrate category history")).classes("section-title")
                ui.label(
                    translate("Preview an explicit mapping before any records change.")
                ).classes("muted text-xs")
            with ui.row().classes("items-center gap-0"):
                ui.button(icon="help_outline", on_click=self.help_dialog.open).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Migration help')}'"
                )
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )

        with ui.column().classes("category-migration-body gap-4 w-full"):
            with ui.element("div").classes("category-migration-grid"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(translate("Past category")).classes("field-label mb-0")
                    ui.select(
                        categories, value=self.source_category_id,
                        on_change=lambda event: self._set_source(event.value),
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("w-full")
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(translate("Past subcategory")).classes("field-label mb-0")
                    source_options = {
                        ALL_SUBCATEGORIES: translate("All subcategories"),
                        **{
                            row["label"]: (
                                f'{row["label"]} · {row["transaction_count"]}'
                                if row["transaction_count"] else row["label"]
                            )
                            for row in source_subcategories
                        },
                    }
                    ui.select(
                        source_options,
                        value=self.source_subcategory or ALL_SUBCATEGORIES,
                        on_change=lambda event: self._set_source_subcategory(event.value),
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("w-full")
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(translate("New category")).classes("field-label mb-0")
                    ui.select(
                        active, value=self.target_category_id,
                        on_change=lambda event: self._set_target(event.value),
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("w-full")
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(translate("New subcategory")).classes("field-label mb-0")
                    ui.select(
                        {
                            "keep": translate("Keep each existing value"),
                            "clear": translate("Clear subcategory"),
                            "replace": translate("Replace with one value"),
                        },
                        value=self.target_action,
                        on_change=lambda event: self._set_target_action(event.value),
                    ).props(
                        "outlined dense options-dense popup-content-class=theme-select-menu"
                    ).classes("w-full")
                    if self.target_action == "replace":
                        ui.select(
                            target_subcategories,
                            value=self.target_subcategory or None,
                            label=translate("Target subcategory"), with_input=True,
                            new_value_mode="add-unique", clearable=True,
                            on_change=lambda event: (
                                setattr(self, "target_subcategory", event.value or ""),
                                self._changed(),
                            ),
                        ).props(
                            "outlined dense options-dense popup-content-class=theme-select-menu"
                        ).classes("w-full mt-1")

            with ui.row().classes("items-center justify-between w-full"):
                ui.label(
                    translate("Nothing changes until you confirm the preview.")
                ).classes("muted text-xs")
                primary_action("Preview mapping", "preview", self._preview)

            if self.error_message:
                ui.label(self.error_message).classes("field-error")

            if self.preview:
                with ui.element("section").classes("category-migration-preview"):
                    with ui.column().classes("gap-0"):
                        ui.label(translate("Preview")).classes("field-label mb-0")
                        date_range = (
                            f'{date_label(self.preview["first_date"], include_year=True)} – '
                            f'{date_label(self.preview["last_date"], include_year=True)}'
                            if self.preview["first_date"] else translate("No transaction dates")
                        )
                        ui.label(date_range).classes("muted text-xs")
                    for label, value in (
                        ("Transactions", str(self.preview["transaction_count"])),
                        ("Net historical amount", money(self.preview["amount_cents"])),
                        ("Learned import choices", str(self.preview["vendor_mapping_count"])),
                    ):
                        with ui.column().classes("gap-0"):
                            ui.label(translate(label)).classes("muted text-xs")
                            ui.label(value).classes("font-medium")
                    ui.button(
                        translate("Apply mapping"), icon="swap_horiz", on_click=self._apply,
                    ).props("unelevated no-caps color=negative")

            history = self.repository.category_migration_history(6)
            if history:
                with ui.column().classes("gap-1 w-full"):
                    ui.label(translate("Recent mappings")).classes("field-label mb-1")
                    for record in history:
                        target_detail = (
                            f' · {record["target_subcategory"]}'
                            if record["target_subcategory_action"] == "replace" else ""
                        )
                        source_detail = (
                            f' · {record["source_subcategory"]}'
                            if record["source_subcategory"] else ""
                        )
                        with ui.element("div").classes("category-migration-history-row"):
                            ui.label(
                                f'{record["source_category"]}{source_detail} → '
                                f'{record["target_category"]}{target_detail}'
                            ).classes("text-sm")
                            ui.label(
                                f'{record["affected_transactions"]} transactions · '
                                f'{record["created_at"][:10]}'
                            ).classes("muted text-xs")


class CategoryManagerDialog:
    """Manage future category choices without silently rewriting history."""

    def __init__(
        self,
        repository: Repository,
        migration_dialog: CategoryMigrationDialog,
        on_changed: Callable[[], None],
    ) -> None:
        self.repository = repository
        self.migration_dialog = migration_dialog
        self.on_changed = on_changed
        self.error_message = ""
        self.dialog = ui.dialog()
        with self.dialog:
            self.card = ui.card().classes("category-manager-card")
        self._content_rendered = False

        with ui.dialog() as self.add_dialog, ui.card().classes("confirm-card"):
            ui.label(translate("Add category")).classes("section-title")
            self.new_category_name = ui.input(
                label=translate("Category name")
            ).props("outlined dense maxlength=60").classes("w-full")
            self.new_subcategories = ui.input(
                label=translate("Subcategories"),
                placeholder=translate("Optional · separate with commas"),
            ).props("outlined dense").classes("w-full")
            self.add_error = ui.label("").classes("field-error")
            self.add_error.set_visibility(False)
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(translate("Cancel"), on_click=self.add_dialog.close).props(
                    "flat no-caps color=grey-7"
                )
                primary_action("Add category", "add", self._add_category)

        with ui.dialog() as self.subcategory_dialog, ui.card().classes("confirm-card"):
            self.subcategory_heading = ui.label("").classes("section-title")
            self.new_subcategory_name = ui.input(
                label=translate("Subcategory name")
            ).props("outlined dense maxlength=80").classes("w-full")
            self.subcategory_error = ui.label("").classes("field-error")
            self.subcategory_error.set_visibility(False)
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(
                    translate("Cancel"), on_click=self.subcategory_dialog.close,
                ).props("flat no-caps color=grey-7")
                primary_action("Add subcategory", "add", self._add_subcategory)
        self.subcategory_category_id: int | None = None

        with ui.dialog() as self.rename_dialog, ui.card().classes("confirm-card"):
            self.rename_heading = ui.label("").classes("section-title")
            ui.label(
                translate(
                    "If this category has history, changing its name creates a new category and archives the old one. Historical records stay unchanged until you map them."
                )
            ).classes("muted text-sm")
            self.rename_name = ui.input(
                label=translate("New category name")
            ).props("outlined dense maxlength=60").classes("w-full")
            self.rename_error = ui.label("").classes("field-error")
            self.rename_error.set_visibility(False)
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(translate("Cancel"), on_click=self.rename_dialog.close).props(
                    "flat no-caps color=grey-7"
                )
                primary_action("Save name", "check", self._rename_category)
        self.rename_category_id: int | None = None

    @staticmethod
    def _set_error(label, message: str = "") -> None:
        label.text = message
        label.set_visibility(bool(message))

    def open(self) -> None:
        self.error_message = ""
        if self._content_rendered:
            self.content.refresh()
        else:
            with self.card:
                self.content()
            self._content_rendered = True
        self.dialog.open()

    def _open_add(self) -> None:
        self.new_category_name.value = ""
        self.new_subcategories.value = ""
        self._set_error(self.add_error)
        self.add_dialog.open()

    def _add_category(self) -> None:
        subcategories = [
            value.strip() for value in (self.new_subcategories.value or "").split(",")
            if value.strip()
        ]
        try:
            self.repository.add_category(self.new_category_name.value, subcategories)
        except ValueError as error:
            self._set_error(self.add_error, str(error))
            return
        self.add_dialog.close()
        self.on_changed()
        self.content.refresh()
        ui.notify(translate("Category added"), color="positive", position="bottom")

    def _open_subcategory(self, category: dict) -> None:
        self.subcategory_category_id = category["id"]
        self.subcategory_heading.text = translate(
            "Add a subcategory to {category}", category=category["name"],
        )
        self.new_subcategory_name.value = ""
        self._set_error(self.subcategory_error)
        self.subcategory_dialog.open()

    def _add_subcategory(self) -> None:
        try:
            self.repository.add_subcategory(
                int(self.subcategory_category_id), self.new_subcategory_name.value,
            )
        except (TypeError, ValueError) as error:
            self._set_error(self.subcategory_error, str(error))
            return
        self.subcategory_dialog.close()
        self.on_changed()
        self.content.refresh()
        ui.notify(translate("Subcategory added"), color="positive", position="bottom")

    def _open_rename(self, category: dict) -> None:
        self.rename_category_id = category["id"]
        self.rename_heading.text = translate(
            "Change {category}", category=category["name"],
        )
        self.rename_name.value = category["name"]
        self._set_error(self.rename_error)
        self.rename_dialog.open()

    async def _rename_category(self) -> None:
        try:
            result = self.repository.replace_category_name(
                int(self.rename_category_id), self.rename_name.value,
            )
        except (TypeError, ValueError) as error:
            self._set_error(self.rename_error, str(error))
            return
        self.rename_dialog.close()
        self.on_changed()
        self.content.refresh()
        if not result["history_preserved"]:
            ui.notify(translate("Category name updated"), color="positive", position="bottom")
            return
        with ui.dialog() as prompt, ui.card().classes("confirm-card"):
            ui.label(translate("Historical categories were preserved")).classes("section-title")
            ui.label(
                translate(
                    "Existing transactions still use the old category. You can leave them unchanged or map them now."
                )
            ).classes("muted text-sm")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(
                    translate("Leave history unchanged"),
                    on_click=lambda: prompt.submit("later"),
                ).props("flat no-caps color=grey-7")
                primary_action(
                    "Map history now", "swap_horiz",
                    lambda: prompt.submit("map"),
                )
        if await prompt == "map":
            self.dialog.close()
            self.migration_dialog.open(result["source_id"], result["id"])

    def _move(self, identifier: int, direction: int) -> None:
        try:
            self.repository.move_category(identifier, direction)
        except ValueError as error:
            self.error_message = str(error)
        else:
            self.error_message = ""
            self.on_changed()
        self.content.refresh()

    async def _archive(self, category: dict) -> None:
        with ui.dialog() as prompt, ui.card().classes("confirm-card"):
            ui.label(
                translate("Archive {category}?", category=category["name"])
            ).classes("section-title")
            ui.label(
                translate(
                    "It will disappear from new expense and budget choices. Its {count} historical transactions remain unchanged.",
                    count=category["transaction_count"],
                )
            ).classes("muted text-sm")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(translate("Cancel"), on_click=prompt.close).props(
                    "flat no-caps color=grey-7"
                )
                ui.button(
                    translate("Archive and keep history"),
                    on_click=lambda: prompt.submit("keep"),
                ).props("flat no-caps")
                if category["transaction_count"]:
                    primary_action(
                        "Archive and map history", "swap_horiz",
                        lambda: prompt.submit("map"),
                    )
        action = await prompt
        if not action:
            return
        try:
            self.repository.set_category_active(category["id"], False)
        except ValueError as error:
            self.error_message = str(error)
            self.content.refresh()
            return
        self.on_changed()
        self.content.refresh()
        if action == "map":
            self.dialog.close()
            self.migration_dialog.open(category["id"])

    def _restore(self, identifier: int) -> None:
        try:
            self.repository.set_category_active(identifier, True)
        except ValueError as error:
            self.error_message = str(error)
        else:
            self.error_message = ""
            self.on_changed()
        self.content.refresh()

    def _toggle_subcategory(self, subcategory: dict) -> None:
        try:
            self.repository.set_subcategory_active(
                subcategory["id"], not bool(subcategory["is_active"]),
            )
        except ValueError as error:
            self.error_message = str(error)
        else:
            self.error_message = ""
            self.on_changed()
        self.content.refresh()

    def _open_migration(self) -> None:
        self.dialog.close()
        self.migration_dialog.open()

    @ui.refreshable
    def content(self) -> None:
        library = self.repository.category_library()
        active = [category for category in library if category["is_active"]]
        archived = [category for category in library if not category["is_active"]]

        with ui.row().classes("editor-head items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(translate("Categories & subcategories")).classes("section-title")
                ui.label(
                    translate("Customize future entry without silently changing the past.")
                ).classes("muted text-xs")
            ui.button(icon="close", on_click=self.dialog.close).props(
                f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
            )

        with ui.column().classes("category-manager-body gap-3 w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(
                    translate("{count} active categories", count=len(active))
                ).classes("muted text-sm")
                with ui.row().classes("items-center gap-1"):
                    ui.button(
                        translate("Migrate history"), icon="swap_horiz",
                        on_click=self._open_migration,
                    ).props("flat dense no-caps")
                    primary_action("Add category", "add", self._open_add)
            if self.error_message:
                ui.label(self.error_message).classes("field-error")

            with ui.column().classes("category-definition-list gap-2 w-full"):
                for index, category in enumerate(active):
                    self._category_row(category, index, len(active))

            if archived:
                ui.separator().classes("my-1")
                ui.label(translate("Archived categories")).classes("field-label mb-0")
                with ui.column().classes("category-definition-list archived gap-2 w-full"):
                    for category in archived:
                        self._category_row(category, 0, 0)

    def _category_row(self, category: dict, index: int, active_count: int) -> None:
        with ui.element("section").classes(
            f'category-definition {"archived" if not category["is_active"] else ""}'
        ):
            with ui.row().classes("items-start justify-between gap-3 w-full"):
                with ui.row().classes("items-start gap-2 min-w-0"):
                    if category["is_active"]:
                        with ui.column().classes("category-order-controls gap-0"):
                            up = ui.button(
                                icon="keyboard_arrow_up",
                                on_click=lambda _, identifier=category["id"]: self._move(identifier, -1),
                            ).props("flat round dense color=grey-6 aria-label='Move category up'")
                            down = ui.button(
                                icon="keyboard_arrow_down",
                                on_click=lambda _, identifier=category["id"]: self._move(identifier, 1),
                            ).props("flat round dense color=grey-6 aria-label='Move category down'")
                            if index == 0:
                                up.disable()
                            if index == active_count - 1:
                                down.disable()
                    with ui.column().classes("gap-0 min-w-0"):
                        ui.label(category["name"]).classes("font-medium")
                        ui.label(
                            translate(
                                "{count} historical transactions",
                                count=category["transaction_count"],
                            )
                        ).classes("muted text-xs")
                with ui.row().classes("items-center gap-0 no-wrap"):
                    if category["is_active"]:
                        ui.button(
                            icon="edit_outlined",
                            on_click=lambda _, record=category: self._open_rename(record),
                        ).props("flat round dense color=grey-6 aria-label='Change category name'")
                        ui.button(
                            icon="archive",
                            on_click=lambda _, record=category: self._archive(record),
                        ).props("flat round dense color=grey-6 aria-label='Archive category'")
                    else:
                        ui.button(
                            translate("Restore"), icon="unarchive",
                            on_click=lambda _, identifier=category["id"]: self._restore(identifier),
                        ).props("flat dense no-caps")

            if category["is_active"]:
                active_subcategories = [
                    item for item in category["subcategories"] if item["is_active"]
                ]
                inactive_subcategories = [
                    item for item in category["subcategories"] if not item["is_active"]
                ]
                with ui.row().classes("category-subcategory-list items-center gap-1 w-full"):
                    for subcategory in active_subcategories:
                        with ui.button(
                            on_click=lambda _, record=subcategory: self._toggle_subcategory(record),
                        ).props("flat dense no-caps").classes("subcategory-definition-chip"):
                            ui.label(subcategory["name"])
                            ui.icon("close").classes("text-xs")
                    if inactive_subcategories:
                        ui.label(
                            translate(
                                "+{count} archived", count=len(inactive_subcategories),
                            )
                        ).classes("muted text-xs")
                        for subcategory in inactive_subcategories:
                            ui.button(
                                subcategory["name"],
                                on_click=lambda _, record=subcategory: self._toggle_subcategory(record),
                            ).props("flat dense no-caps").classes("subcategory-restore")
                    ui.button(
                        translate("Add subcategory"), icon="add",
                        on_click=lambda _, record=category: self._open_subcategory(record),
                    ).props("flat dense no-caps").classes("subcategory-add")
