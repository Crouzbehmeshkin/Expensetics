from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
import sys

from nicegui import ui

from .category_ui import CategoryManagerDialog, CategoryMigrationDialog
from .components import primary_action
from .db import DATA_DIR, DB_PATH
from .device_unlock import (
    DeviceUnlockError, DeviceUnlockStore, enrollment_script, platform_authenticator_name,
    result_error, rewrap_script,
)
from .export import export_encrypted_backup, restore_encrypted_backup
from .i18n import translate
from .repository import Repository
from .session_security import AuthorizationExpired, remove_storage_secret
from .vault import (
    VaultError, change_password, database_password, delete_vault, remove_legacy_csvs,
)


class SettingsDialog:
    def __init__(
        self,
        repository: Repository,
        on_restored: Callable[[], None],
        lock_all_clients: Callable[..., None],
    ) -> None:
        self.repository = repository
        self.on_restored = on_restored
        self.lock_all_clients = lock_all_clients
        self.device_store = DeviceUnlockStore(DATA_DIR)
        self.category_migration = CategoryMigrationDialog(
            repository, self._categories_changed,
        )
        self.category_manager = CategoryManagerDialog(
            repository, self.category_migration, self._categories_changed,
        )
        with ui.dialog() as self.dialog, ui.card().classes("settings-card"):
            with ui.row().classes("editor-head items-center justify-between w-full"):
                ui.label(translate("Settings")).classes("section-title")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    f"flat round dense color=grey-7 aria-label='{translate('Close')}'"
                )
            with ui.column().classes("editor-body gap-4 w-full"):
                self._category_section()
                ui.separator()
                self._backup_section()
                ui.separator()
                self._security_section()
                ui.separator()
                self._deletion_section()

    def _category_section(self) -> None:
        with ui.column().classes("gap-3 w-full"):
            ui.label(translate("Categories & subcategories")).classes("field-label mb-0")
            ui.label(
                translate(
                    "Create, order, and archive the categories shown during entry and budgeting. Historical records change only through an explicit migration."
                )
            ).classes("muted text-xs")
            with ui.row().classes("device-unlock-status items-center gap-2 w-full"):
                ui.icon("category").classes("text-base")
                self.category_summary = ui.label("").classes("text-sm")
            with ui.row().classes("justify-end gap-1 w-full"):
                ui.button(
                    translate("Migrate history"), icon="swap_horiz",
                    on_click=lambda: self.category_migration.open(),
                ).props("flat no-caps")
                primary_action(
                    "Manage categories", "tune", self.category_manager.open,
                )

    def _backup_section(self) -> None:
        with ui.column().classes("gap-3 w-full"):
            ui.label(translate("Encrypted backup")).classes("field-label mb-0")
            ui.label(
                "Backups are created only when requested. They contain no readable CSV files and use a separate password."
            ).classes("muted text-xs")
            self.export_path = ui.input(
                placeholder=translate("Full path ending in .expensetics")
            ).props("outlined dense").classes("w-full settings-path")
            with ui.row().classes("w-full gap-3 entry-primary-row"):
                self.export_password = ui.input(
                    translate("Backup password"), password=True, password_toggle_button=True,
                ).props("outlined dense").classes("flex-1")
                self.export_confirmation = ui.input(
                    translate("Confirm password"), password=True,
                ).props("outlined dense").classes("flex-1")
            self.export_error = self._error_label()
            with ui.row().classes("justify-end w-full"):
                primary_action("Create encrypted backup", "lock", self.export_backup)

            ui.separator().classes("my-1")
            ui.label(translate("Restore encrypted backup")).classes("field-label mb-0")
            ui.label(
                "Restore replaces the current vault after the backup is decrypted and integrity-checked."
            ).classes("muted text-xs")
            self.restore_path = ui.input(
                placeholder=translate("Full path to an .expensetics backup")
            ).props("outlined dense").classes("w-full settings-path")
            self.restore_password = ui.input(
                translate("Backup password"), password=True, password_toggle_button=True,
            ).props("outlined dense").classes("w-full")
            self.restore_confirmation = ui.checkbox(
                translate("I understand this replaces the current local data"), value=False,
            ).props("dense color=negative")
            self.restore_error = self._error_label()
            with ui.row().classes("justify-end w-full"):
                ui.button(
                    translate("Restore backup"), icon="restore", on_click=self.restore_backup,
                ).props("flat no-caps color=negative")

    def _security_section(self) -> None:
        with ui.column().classes("gap-3 w-full"):
            ui.label(translate("Device unlock")).classes("field-label mb-0")
            ui.label(
                translate(
                    "Use {authenticator} to release a device-protected key. Your app password and encrypted backups remain available."
                ).format(authenticator=translate(platform_authenticator_name()))
            ).classes("muted text-xs")
            with ui.row().classes("device-unlock-status items-center gap-2 w-full"):
                ui.icon("fingerprint").classes("text-base")
                self.device_status = ui.label("").classes("text-sm")
            self.device_error = self._error_label()
            with ui.row().classes("justify-end w-full"):
                self.device_enable_button = primary_action(
                    "Set up device unlock", "fingerprint", self.request_device_unlock,
                )
                self.device_remove_button = ui.button(
                    translate("Remove device unlock"),
                    on_click=self.remove_device_unlock,
                ).props("flat no-caps color=negative")

            with ui.dialog() as self.device_setup_dialog, ui.card().classes("confirm-card"):
                ui.label(translate("Make Windows Hello the default")).classes("section-title")
                ui.label(
                    translate(
                        "Expensetics requests this Windows device, but Windows chooses among the passkey services enabled on your PC."
                    )
                ).classes("muted text-sm")
                ui.label(
                    translate(
                        "In Windows Settings, open Accounts > Passkeys > Advanced options. Keep Save passkeys to this Windows device on and turn Microsoft Password Manager off."
                    )
                ).classes("muted text-sm")
                ui.label(
                    translate(
                        "Otherwise, choose Save another way in the Windows prompt, then Windows Hello or This Windows device."
                    )
                ).classes("muted text-sm")
                ui.label(
                    translate(
                        "Use your normal Windows sign-in PIN or biometric. Expensetics never receives or stores it."
                    )
                ).classes("muted text-sm")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button(
                        translate("Cancel"), on_click=self.device_setup_dialog.close,
                    ).props("flat no-caps color=grey-7")
                    primary_action(
                        "Continue to Windows Hello", "fingerprint",
                        self.confirm_device_unlock,
                    )
            ui.separator().classes("my-1")
            ui.label(translate("Change app password")).classes("field-label mb-0")
            ui.label(
                "The password is the SQLCipher database key. It is never stored in readable form."
            ).classes("muted text-xs")
            self.current_password = ui.input(
                translate("Current password"), password=True,
            ).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full gap-3 entry-primary-row"):
                self.new_password = ui.input(
                    translate("New password"), password=True,
                ).props("outlined dense").classes("flex-1")
                self.new_password_confirmation = ui.input(
                    translate("Confirm password"), password=True,
                ).props("outlined dense").classes("flex-1")
            self.password_error = self._error_label()
            with ui.row().classes("justify-end w-full"):
                primary_action("Change password", "key", self.update_password)
        self._refresh_device_controls()

    async def request_device_unlock(self) -> None:
        if sys.platform.startswith("win"):
            self.device_setup_dialog.open()
            return
        await self.enable_device_unlock()

    async def confirm_device_unlock(self) -> None:
        self.device_setup_dialog.close()
        await self.enable_device_unlock()

    def _deletion_section(self) -> None:
        with ui.column().classes("gap-2 w-full danger-zone"):
            ui.label(translate("Delete local financial data")).classes("field-label mb-0 negative")
            ui.label(
                "Deletes the encrypted vault, device unlock, and legacy CSV mirrors on this device. Separate backups are not deleted."
            ).classes("muted text-xs")
            with ui.row().classes("justify-end w-full"):
                ui.button(
                    translate("Delete all local data"), icon="delete_forever",
                    on_click=self.open_delete_confirmation,
                ).props("flat no-caps color=negative")

        with ui.dialog() as self.delete_dialog, ui.card().classes("confirm-card"):
            ui.label(translate("Delete all local data?")).classes("section-title")
            ui.label(
                "This cannot be undone. Type DELETE to remove the encrypted database from this device."
            ).classes("muted text-sm")
            self.delete_text = ui.input(placeholder="DELETE").props("outlined dense").classes("w-full")
            self.delete_error = self._error_label()
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button(translate("Cancel"), on_click=self.delete_dialog.close).props(
                    "flat no-caps color=grey-7"
                )
                ui.button(
                    translate("Delete permanently"), on_click=self.delete_all_data,
                ).props("unelevated no-caps color=negative")

    @staticmethod
    def _error_label():
        label = ui.label("").classes("field-error")
        label.set_visibility(False)
        return label

    def open(self) -> None:
        default_backup = Path.home() / "Documents" / f"Expensetics-{date.today():%Y-%m-%d}.expensetics"
        self.export_path.value = str(default_backup)
        self.restore_path.value = ""
        for field in (
            self.export_password, self.export_confirmation, self.restore_password,
            self.current_password, self.new_password, self.new_password_confirmation,
        ):
            field.value = ""
        self.restore_confirmation.value = False
        for error in (
            self.export_error, self.restore_error, self.password_error, self.device_error,
        ):
            self._set_error(error)
        self._refresh_category_summary()
        self._refresh_device_controls()
        self.dialog.open()

    def _categories_changed(self) -> None:
        self._refresh_category_summary()
        self.on_restored()

    def _refresh_category_summary(self) -> None:
        library = self.repository.category_library()
        active = [category for category in library if category["is_active"]]
        subcategories = sum(
            1 for category in active for item in category["subcategories"]
            if item["is_active"]
        )
        self.category_summary.text = translate(
            "{categories} active categories · {subcategories} active subcategories",
            categories=len(active), subcategories=subcategories,
        )

    def export_backup(self) -> None:
        path = (self.export_path.value or "").strip()
        password = self.export_password.value or ""
        if not path:
            self._set_error(self.export_error, "Choose where to save the backup.")
            return
        if password != (self.export_confirmation.value or ""):
            self._set_error(self.export_error, "Passwords do not match.")
            return
        try:
            with self.repository.authorization():
                destination = export_encrypted_backup(path, password)
        except AuthorizationExpired:
            raise
        except (ValueError, RuntimeError, OSError) as error:
            self._set_error(self.export_error, f"Could not create backup: {error}")
            return
        self._set_error(self.export_error)
        self.export_password.value = self.export_confirmation.value = ""
        ui.notify(f"Encrypted backup created at {destination}", color="positive", timeout=3500)

    def restore_backup(self) -> None:
        path = (self.restore_path.value or "").strip()
        if not path:
            self._set_error(self.restore_error, "Choose an .expensetics backup.")
            return
        if not self.restore_confirmation.value:
            self._set_error(self.restore_error, "Confirm that the current local data will be replaced.")
            return
        try:
            with self.repository.maintenance():
                restore_encrypted_backup(
                    path, self.restore_password.value or "",
                )
        except AuthorizationExpired:
            raise
        except (ValueError, RuntimeError, OSError) as error:
            self._set_error(self.restore_error, f"Could not restore backup: {error}")
            return
        self.restore_password.value = ""
        self.restore_confirmation.value = False
        self.dialog.close()
        self.on_restored()
        ui.notify("Encrypted backup restored", color="positive", timeout=3000)

    async def enable_device_unlock(self) -> None:
        with self.repository.authorization():
            password = database_password()
        if password is None:
            self._set_error(self.device_error, translate("Unlock the app before setting up device unlock."))
            return
        try:
            result = await ui.run_javascript(enrollment_script(password), timeout=135.0)
            message = result_error(result)
            if message:
                raise DeviceUnlockError(translate(message))
            with self.repository.authorization():
                self.device_store.save(result["record"])
        except (DeviceUnlockError, KeyError, TimeoutError, OSError) as error:
            self._set_error(self.device_error, str(error))
            return
        self._set_error(self.device_error)
        self._refresh_device_controls()
        ui.notify(translate("Device unlock is ready"), color="positive", position="bottom")

    def remove_device_unlock(self) -> None:
        try:
            with self.repository.authorization():
                self.device_store.remove()
        except DeviceUnlockError as error:
            self._set_error(self.device_error, str(error))
            return
        self._set_error(self.device_error)
        self._refresh_device_controls()
        ui.notify(translate("Device unlock removed"), position="bottom")

    async def update_password(self) -> None:
        enrolled = self.device_store.is_enrolled()
        new_password = self.new_password.value or ""
        try:
            with self.repository.maintenance():
                change_password(
                    DB_PATH,
                    self.current_password.value or "",
                    new_password,
                    self.new_password_confirmation.value or "",
                )
        except AuthorizationExpired:
            raise
        except (ValueError, RuntimeError) as error:
            self._set_error(self.password_error, str(error))
            return

        device_warning = ""
        if enrolled:
            try:
                record = self.device_store.load()
                result = await ui.run_javascript(
                    rewrap_script(record, new_password), timeout=75.0,
                )
                message = result_error(result)
                if message:
                    raise DeviceUnlockError(translate(message))
                with self.repository.authorization():
                    self.device_store.save(result["record"])
            except AuthorizationExpired:
                try:
                    self.device_store.remove()
                except DeviceUnlockError:
                    pass
                raise
            except (DeviceUnlockError, KeyError, TimeoutError, OSError):
                try:
                    self.device_store.remove()
                except DeviceUnlockError:
                    device_warning = translate(
                        "Device unlock could not be updated. Remove it before using it again."
                    )
                else:
                    device_warning = translate(
                        "Device unlock was removed because it could not be updated. Set it up again if wanted."
                    )
        self._set_error(self.password_error)
        self.current_password.value = self.new_password.value = self.new_password_confirmation.value = ""
        ui.notify("App password changed", color="positive", position="bottom")
        self._refresh_device_controls()
        if device_warning:
            ui.notify(device_warning, color="warning", position="bottom", timeout=4500)

    def open_delete_confirmation(self) -> None:
        self.delete_text.value = ""
        self._set_error(self.delete_error)
        self.delete_dialog.open()

    def delete_all_data(self) -> None:
        if self.delete_text.value != "DELETE":
            self._set_error(self.delete_error, "Type DELETE exactly to continue.")
            return
        try:
            with self.repository.authorization():
                pass
        except AuthorizationExpired:
            raise
        self.lock_all_clients(redirect=False)

        failures: list[str] = []
        for label, operation in (
            ("vault", lambda: delete_vault(DB_PATH)),
            ("device unlock", self.device_store.remove),
            ("legacy CSV files", lambda: remove_legacy_csvs(DATA_DIR)),
            ("browser storage secret", lambda: remove_storage_secret(DATA_DIR)),
        ):
            try:
                operation()
            except (DeviceUnlockError, VaultError, OSError):
                failures.append(label)
        if failures:
            error = ", ".join(failures)
            self._set_error(self.delete_error, f"Could not remove all local data: {error}")
            return
        self.delete_dialog.close()
        self.dialog.close()
        self.lock_all_clients()

    def _refresh_device_controls(self) -> None:
        enrolled = self.device_store.is_enrolled()
        self.device_status.text = translate(
            "Ready on this device" if enrolled else "Not set up"
        )
        self.device_enable_button.set_visibility(not enrolled)
        self.device_remove_button.set_visibility(enrolled)

    @staticmethod
    def _set_error(label, message: str = "") -> None:
        label.text = message
        label.set_visibility(bool(message))
