from __future__ import annotations

import base64
import json

import pytest

from finance_app.device_unlock import (
    DEVICE_UNLOCK_FILENAME, DeviceUnlockError, DeviceUnlockStore, enrollment_script,
    platform_authenticator_name, result_error,
)


def encoded(size: int, byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * size).decode().rstrip("=")


def record() -> dict[str, object]:
    return {
        "version": 2,
        "credential_id": encoded(32, 1),
        "salt": encoded(32, 2),
        "iv": encoded(12, 3),
        "ciphertext": encoded(40, 4),
        "rp_id": "127.0.0.1",
        "transports": ["internal"],
    }


def test_device_unlock_store_round_trips_only_a_valid_encrypted_envelope(tmp_path) -> None:
    store = DeviceUnlockStore(tmp_path)

    saved = store.save(record())

    assert store.path == tmp_path / DEVICE_UNLOCK_FILENAME
    assert store.is_enrolled()
    assert store.load() == saved
    persisted = store.path.read_text(encoding="utf-8")
    assert json.loads(persisted) == record()
    assert "database password" not in persisted
    assert store.remove()
    assert not store.is_enrolled()
    assert not store.remove()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 3),
        ("version", "1"),
        ("credential_id", "not base64!"),
        ("salt", encoded(31, 2)),
        ("iv", encoded(16, 3)),
        ("ciphertext", encoded(16, 4)),
        ("rp_id", "https://127.0.0.1/path"),
        ("transports", []),
        ("transports", ["internal", "internal"]),
        ("transports", ["not valid!"]),
    ],
)
def test_device_unlock_store_rejects_malformed_records(tmp_path, field, value) -> None:
    value_to_save = record()
    value_to_save[field] = value
    with pytest.raises(DeviceUnlockError):
        DeviceUnlockStore(tmp_path).save(value_to_save)


def test_corrupt_device_unlock_record_fails_closed(tmp_path) -> None:
    store = DeviceUnlockStore(tmp_path)
    store.path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(DeviceUnlockError, match="could not be read"):
        store.load()


def test_original_device_record_uses_safe_platform_transport_hint(tmp_path) -> None:
    original = record()
    original["version"] = 1
    original.pop("transports")
    path = tmp_path / DEVICE_UNLOCK_FILENAME
    path.write_text(json.dumps(original), encoding="utf-8")

    loaded = DeviceUnlockStore(tmp_path).load()

    assert loaded.version == 1
    assert loaded.transports == ("internal",)


def test_browser_contract_is_json_encoded_and_errors_are_bounded() -> None:
    script = enrollment_script("quotes ' and \" remain data")
    assert script.startswith("return await window.expenseticsDeviceUnlock.enroll(")
    assert result_error({"ok": True}) is None
    assert "cancelled" in result_error({"ok": False, "error": "cancelled"}).lower()
    assert result_error({"ok": False, "error": "unexpected"}) == (
        "Device unlock could not be completed."
    )
    assert result_error({"ok": False, "error": "invalid_origin"}) == (
        "Open Expensetics at http://localhost using the same port, then try again."
    )


def test_platform_authenticator_names_are_explicit() -> None:
    assert platform_authenticator_name("win32") == "Windows Hello"
    assert platform_authenticator_name("darwin") == "Touch ID or Mac password"
    assert platform_authenticator_name("linux") == "this device"
