from threading import Event, Thread

import pytest

import finance_app.session_security as security_module
from finance_app.db import DB_PATH, authorization_required, initialize
from finance_app.repository import Repository
from finance_app.session_security import (
    AuthorizationExpired, authorization_lease, authorize_session,
    has_authorized_sessions, local_host, maintenance_lease, remove_storage_secret,
    request_host_is_local, revoke_all_sessions, session_is_authorized, storage_secret,
    touch_session,
)


def test_browser_authorization_is_process_scoped_and_globally_revocable() -> None:
    first: dict[str, object] = {}
    second: dict[str, object] = {}

    authorize_session(first)
    assert session_is_authorized(first)
    assert not session_is_authorized(second)

    authorize_session(second)
    assert session_is_authorized(first)
    assert session_is_authorized(second)

    revoke_all_sessions(first)
    assert not session_is_authorized(first)
    assert not session_is_authorized(second)


def test_browser_authorization_expires_after_ten_minutes_of_inactivity(monkeypatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(security_module, "monotonic", lambda: clock["now"])
    storage: dict[str, object] = {}
    permit = authorize_session(storage)

    clock["now"] += 599
    assert session_is_authorized(storage)
    assert touch_session(storage)
    clock["now"] += 600
    assert not session_is_authorized(storage)
    assert not has_authorized_sessions()
    with pytest.raises(AuthorizationExpired, match="locked"):
        with authorization_lease(permit):
            pass

def test_stale_repository_cannot_revive_after_another_session_unlocks(tmp_path) -> None:
    database = tmp_path / "finance.db"
    initialize(database)
    first_storage: dict[str, object] = {}
    second_storage: dict[str, object] = {}
    first = Repository(
        database,
        permit=authorize_session(first_storage),
        require_authorization=True,
    )
    assert first.categories()

    revoke_all_sessions(first_storage)
    second = Repository(
        database,
        permit=authorize_session(second_storage),
        require_authorization=True,
    )

    with pytest.raises(AuthorizationExpired, match="locked"):
        first.categories()
    assert second.categories()
    revoke_all_sessions(second_storage)


def test_database_access_fails_closed_without_a_permit(tmp_path) -> None:
    database = tmp_path / "finance.db"
    initialize(database)
    protected = Repository(database, require_authorization=True)

    with pytest.raises(AuthorizationExpired, match="locked"):
        protected.categories()


def test_application_database_cannot_opt_out_of_authorization() -> None:
    assert authorization_required(DB_PATH)
    assert authorization_required(DB_PATH, False)


def test_global_revocation_waits_for_an_active_authorized_operation() -> None:
    storage: dict[str, object] = {}
    permit = authorize_session(storage)
    entered = Event()
    release = Event()
    revoked = Event()

    def use_permit() -> None:
        with authorization_lease(permit):
            entered.set()
            assert release.wait(2)

    def revoke() -> None:
        revoke_all_sessions(storage)
        revoked.set()

    worker = Thread(target=use_permit)
    revoker = Thread(target=revoke)
    worker.start()
    assert entered.wait(1)
    revoker.start()
    assert not revoked.wait(0.05)
    release.set()
    worker.join(2)
    revoker.join(2)
    assert not worker.is_alive()
    assert not revoker.is_alive()
    assert revoked.is_set()


def test_maintenance_waits_for_active_work_and_blocks_new_work() -> None:
    storage: dict[str, object] = {}
    permit = authorize_session(storage)
    ordinary_entered = Event()
    release_ordinary = Event()
    maintenance_entered = Event()
    release_maintenance = Event()
    later_entered = Event()

    def ordinary_work() -> None:
        with authorization_lease(permit):
            ordinary_entered.set()
            assert release_ordinary.wait(2)

    def maintenance_work() -> None:
        with maintenance_lease(permit):
            maintenance_entered.set()
            assert release_maintenance.wait(2)

    def later_work() -> None:
        with authorization_lease(permit):
            later_entered.set()

    ordinary = Thread(target=ordinary_work)
    maintenance = Thread(target=maintenance_work)
    later = Thread(target=later_work)
    ordinary.start()
    assert ordinary_entered.wait(1)
    maintenance.start()
    assert not maintenance_entered.wait(0.05)
    release_ordinary.set()
    assert maintenance_entered.wait(1)
    later.start()
    assert not later_entered.wait(0.05)
    release_maintenance.set()
    ordinary.join(2)
    maintenance.join(2)
    later.join(2)
    assert not ordinary.is_alive()
    assert not maintenance.is_alive()
    assert not later.is_alive()
    assert later_entered.is_set()
    revoke_all_sessions(storage)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST"])
def test_local_host_accepts_only_loopback_bindings(host) -> None:
    assert local_host(host)


def test_local_host_defaults_to_webauthn_compatible_localhost() -> None:
    assert local_host() == "localhost"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_local_host_rejects_network_exposure(host) -> None:
    with pytest.raises(ValueError, match="loopback"):
        local_host(host)


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST:8080", "127.0.0.1:49152", "[::1]:8080", "127.7.8.9"],
)
def test_request_host_accepts_only_loopback_names_and_addresses(host) -> None:
    assert request_host_is_local(host)


@pytest.mark.parametrize(
    "host",
    [None, "", "example.com", "localhost.example.com", "192.168.1.10:8080",
     "localhost@evil.test", "localhost/path", "localhost:70000"],
)
def test_request_host_rejects_dns_rebinding_and_invalid_values(host) -> None:
    assert not request_host_is_local(host)


def test_storage_signing_secret_is_random_persistent_and_removable(tmp_path) -> None:
    first = storage_secret(tmp_path)
    assert len(first) >= 32
    assert storage_secret(tmp_path) == first
    assert (tmp_path / "storage-secret").read_text(encoding="ascii") == first

    assert remove_storage_secret(tmp_path)
    replacement = storage_secret(tmp_path)
    assert replacement != first


def test_explicit_storage_secret_override_does_not_write_a_file(tmp_path, monkeypatch) -> None:
    configured = "test-only-configured-storage-secret-1234"
    monkeypatch.setenv("EXPENSETICS_STORAGE_SECRET", configured)
    assert storage_secret(tmp_path) == configured
    assert not (tmp_path / "storage-secret").exists()


def test_explicit_storage_secret_rejects_a_weak_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXPENSETICS_STORAGE_SECRET", "too-short")
    with pytest.raises(ValueError, match="at least 32"):
        storage_secret(tmp_path)
