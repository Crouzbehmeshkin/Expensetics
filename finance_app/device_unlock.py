from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


DEVICE_UNLOCK_FILENAME = "device-unlock.json"
_VERSION = 2
_SUPPORTED_VERSIONS = {1, _VERSION}
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")
_TRANSPORT_PATTERN = re.compile(r"^[a-z0-9-]{1,32}$")


class DeviceUnlockError(RuntimeError):
    """Raised when a stored device-unlock record is invalid or unavailable."""


@dataclass(frozen=True)
class DeviceUnlockRecord:
    """Encrypted password envelope produced by the browser's platform authenticator."""

    version: int
    credential_id: str
    salt: str
    iv: str
    ciphertext: str
    rp_id: str
    transports: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DeviceUnlockRecord:
        try:
            if not isinstance(value["version"], int) or isinstance(value["version"], bool):
                raise TypeError
            text_fields = ("credential_id", "salt", "iv", "ciphertext", "rp_id")
            if any(not isinstance(value[field], str) for field in text_fields):
                raise TypeError
            transports = value.get("transports")
            if transports is None and value["version"] == 1:
                # V1 required a platform authenticator but omitted the transport.
                transports = ["internal"]
            if not isinstance(transports, list) or any(
                not isinstance(transport, str) for transport in transports
            ):
                raise TypeError
            record = cls(
                version=value["version"],
                credential_id=value["credential_id"],
                salt=value["salt"],
                iv=value["iv"],
                ciphertext=value["ciphertext"],
                rp_id=value["rp_id"],
                transports=tuple(transports),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DeviceUnlockError("The device unlock record is invalid") from error
        record.validate()
        return record

    def validate(self) -> None:
        if self.version not in _SUPPORTED_VERSIONS:
            raise DeviceUnlockError("This device unlock record version is not supported")
        if not _HOST_PATTERN.fullmatch(self.rp_id):
            raise DeviceUnlockError("The device unlock record has an invalid host")
        lengths = {
            "credential_id": len(_decode(self.credential_id, "credential ID")),
            "salt": len(_decode(self.salt, "salt")),
            "iv": len(_decode(self.iv, "initialization vector")),
            "ciphertext": len(_decode(self.ciphertext, "ciphertext")),
        }
        if not 16 <= lengths["credential_id"] <= 1024:
            raise DeviceUnlockError("The device unlock credential ID is invalid")
        if lengths["salt"] != 32 or lengths["iv"] != 12:
            raise DeviceUnlockError("The device unlock encryption parameters are invalid")
        if not 17 <= lengths["ciphertext"] <= 4096:
            raise DeviceUnlockError("The device unlock ciphertext is invalid")
        if (
            not 1 <= len(self.transports) <= 8
            or len(set(self.transports)) != len(self.transports)
            or any(not _TRANSPORT_PATTERN.fullmatch(value) for value in self.transports)
        ):
            raise DeviceUnlockError("The device unlock transports are invalid")


class DeviceUnlockStore:
    """Persist only the encrypted envelope; key release stays in WebAuthn."""

    def __init__(self, data_directory: Path) -> None:
        self.path = data_directory / DEVICE_UNLOCK_FILENAME

    def is_enrolled(self) -> bool:
        return self.path.is_file()

    def load(self) -> DeviceUnlockRecord:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise DeviceUnlockError("Device unlock is not set up") from error
        except (OSError, json.JSONDecodeError) as error:
            raise DeviceUnlockError("The device unlock record could not be read") from error
        if not isinstance(value, dict):
            raise DeviceUnlockError("The device unlock record is invalid")
        return DeviceUnlockRecord.from_mapping(value)

    def save(self, value: dict[str, Any]) -> DeviceUnlockRecord:
        record = DeviceUnlockRecord.from_mapping(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.unlink(missing_ok=True)
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.path)
        except OSError as error:
            raise DeviceUnlockError("The device unlock record could not be saved") from error
        finally:
            temporary.unlink(missing_ok=True)
        return record

    def remove(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise DeviceUnlockError("The device unlock record could not be removed") from error
        if self.path.exists():
            raise DeviceUnlockError("The device unlock record could not be removed")
        return True


def enrollment_script(password: str) -> str:
    return _browser_call("enroll", password)


def unlock_script(record: DeviceUnlockRecord) -> str:
    return _browser_call("unlock", asdict(record))


def rewrap_script(record: DeviceUnlockRecord, password: str) -> str:
    return _browser_call("rewrap", asdict(record), password)


def platform_authenticator_name(platform: str | None = None) -> str:
    selected = platform or sys.platform
    if selected.startswith("win"):
        return "Windows Hello"
    if selected == "darwin":
        return "Touch ID or Mac password"
    return "this device"


def result_error(value: Any) -> str | None:
    """Translate a small browser result contract into a safe user-facing message."""
    if isinstance(value, dict) and value.get("ok") is True:
        return None
    code = value.get("error") if isinstance(value, dict) else None
    return {
        "cancelled": (
            "Device verification was cancelled, timed out, or the selected passkey provider "
            "was unavailable. Try again and choose another provider."
        ),
        "decrypt_failed": "This device credential could not unlock the vault.",
        "invalid_origin": (
            "Open Expensetics at http://localhost using the same port, then try again."
        ),
        "not_secure": "Device unlock requires the local secure browser context.",
        "origin_changed": "Open Expensetics using the same local address used during setup.",
        "prf_unsupported": "This browser or device does not support protected device unlock.",
        "unavailable": "No user-verifying platform authenticator is available.",
        "unsupported": "This browser does not support protected device unlock.",
    }.get(code, "Device unlock could not be completed.")


def _decode(value: str, label: str) -> bytes:
    if not value or len(value) > 8192:
        raise DeviceUnlockError(f"The device unlock {label} is invalid")
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise DeviceUnlockError(f"The device unlock {label} is invalid") from error


def _browser_call(method: str, *arguments: Any) -> str:
    encoded = ",".join(json.dumps(argument) for argument in arguments)
    return f"return await window.expenseticsDeviceUnlock.{method}({encoded})"
