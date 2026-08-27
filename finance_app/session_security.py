from __future__ import annotations

import os
from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
import secrets
from threading import Condition
from time import monotonic
from typing import Iterator
from urllib.parse import urlsplit

from starlette.datastructures import Headers


SESSION_KEY = "vault_session_permit"
STORAGE_SECRET_FILENAME = "storage-secret"
INACTIVITY_TIMEOUT_SECONDS = 10 * 60


class AuthorizationExpired(RuntimeError):
    """Raised when a browser session no longer has access to the vault."""


@dataclass(frozen=True)
class AccessPermit:
    token: str


_authorization = Condition()
_authorized_tokens: dict[str, float] = {}
_active_leases = 0
_maintenance_active = False
_revoking = False


def _permit_is_valid(token: str, *, touch: bool = False) -> bool:
    now = monotonic()
    expired = [
        candidate for candidate, last_activity in _authorized_tokens.items()
        if now - last_activity >= INACTIVITY_TIMEOUT_SECONDS
    ]
    for candidate in expired:
        _authorized_tokens.pop(candidate, None)
    if token not in _authorized_tokens:
        return False
    if touch:
        _authorized_tokens[token] = now
    return True


def has_authorized_sessions() -> bool:
    with _authorization:
        if not _authorized_tokens:
            return False
        candidate = next(iter(_authorized_tokens))
        _permit_is_valid(candidate)
        return bool(_authorized_tokens)


def authorize_session(storage: MutableMapping[str, object]) -> AccessPermit:
    """Issue a server-tracked capability to one browser session."""
    with _authorization:
        if _revoking:
            raise AuthorizationExpired("The app is being locked")
        previous = storage.get(SESSION_KEY)
        if isinstance(previous, str):
            _authorized_tokens.pop(previous, None)
        permit = AccessPermit(secrets.token_urlsafe(32))
        _authorized_tokens[permit.token] = monotonic()
        storage[SESSION_KEY] = permit.token
        return permit


def session_permit(storage: MutableMapping[str, object]) -> AccessPermit | None:
    candidate = storage.get(SESSION_KEY)
    return AccessPermit(candidate) if isinstance(candidate, str) else None


def session_is_authorized(storage: MutableMapping[str, object]) -> bool:
    permit = session_permit(storage)
    with _authorization:
        return permit is not None and _permit_is_valid(permit.token) and not _revoking


def touch_session(storage: MutableMapping[str, object]) -> bool:
    """Record real browser activity without weakening server-side expiry checks."""
    permit = session_permit(storage)
    with _authorization:
        return bool(
            permit is not None
            and not _revoking
            and _permit_is_valid(permit.token, touch=True)
        )


@contextmanager
def authorization_lease(permit: AccessPermit | None) -> Iterator[None]:
    """Keep one authorized operation valid until it releases the lease."""
    global _active_leases
    with _authorization:
        while _maintenance_active and not _revoking:
            _authorization.wait()
        if permit is None or _revoking or not _permit_is_valid(permit.token, touch=True):
            raise AuthorizationExpired("This session is locked")
        _active_leases += 1
    try:
        yield
    finally:
        with _authorization:
            _active_leases -= 1
            if _active_leases == 0:
                _authorization.notify_all()


@contextmanager
def maintenance_lease(permit: AccessPermit | None) -> Iterator[None]:
    """Run a vault-wide operation after ordinary authorized work drains."""
    global _maintenance_active
    with _authorization:
        while _maintenance_active and not _revoking:
            _authorization.wait()
        if permit is None or _revoking or not _permit_is_valid(permit.token, touch=True):
            raise AuthorizationExpired("This session is locked")
        _maintenance_active = True
        while _active_leases and not _revoking:
            _authorization.wait()
        if _revoking or not _permit_is_valid(permit.token, touch=True):
            _maintenance_active = False
            _authorization.notify_all()
            raise AuthorizationExpired("This session is locked")
    try:
        yield
    finally:
        with _authorization:
            _maintenance_active = False
            _authorization.notify_all()


def revoke_session(storage: MutableMapping[str, object]) -> None:
    candidate = storage.pop(SESSION_KEY, None)
    if isinstance(candidate, str):
        with _authorization:
            _authorized_tokens.pop(candidate, None)


def revoke_all_sessions(storage: MutableMapping[str, object] | None = None) -> None:
    """Invalidate all capabilities and wait for in-flight data access to finish."""
    global _revoking
    with _authorization:
        _revoking = True
        _authorized_tokens.clear()
        if storage is not None:
            storage.pop(SESSION_KEY, None)
        while _active_leases or _maintenance_active:
            _authorization.wait()
        _revoking = False
        _authorization.notify_all()


def local_host(value: str | None = None) -> str:
    """Choose a WebAuthn-compatible default and reject accidental LAN exposure."""
    candidate = "localhost" if value is None else value.strip()
    if candidate.lower() == "localhost":
        return "localhost"
    try:
        if ip_address(candidate).is_loopback:
            return candidate
    except ValueError:
        pass
    raise ValueError("EXPENSETICS_HOST must be a loopback address")


def request_host_is_local(value: str | None) -> bool:
    """Reject DNS-rebinding Host values while accepting loopback IPv4/IPv6."""
    if not value or any(character.isspace() for character in value):
        return False
    if any(character in value for character in ("/", "\\", "@")):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if hostname is None or (port is not None and not 1 <= port <= 65535):
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


class LoopbackHostMiddleware:
    """Apply the loopback Host policy to HTTP and WebSocket handshakes."""

    def __init__(self, application) -> None:
        self.application = application

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in {"http", "websocket"}:
            host = Headers(scope=scope).get("host")
            if not request_host_is_local(host):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    body = b"Invalid host header"
                    await send({
                        "type": "http.response.start",
                        "status": 400,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                return
        await self.application(scope, receive, send)


def storage_secret(data_directory: Path) -> str:
    """Return a per-install secret used only to sign NiceGUI browser storage."""
    configured = os.environ.get("EXPENSETICS_STORAGE_SECRET")
    if configured:
        if len(configured) < 32:
            raise ValueError("EXPENSETICS_STORAGE_SECRET must contain at least 32 characters")
        return configured

    path = data_directory / STORAGE_SECRET_FILENAME
    try:
        existing = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    if len(existing) >= 32:
        return existing

    data_directory.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("x", encoding="ascii") as handle:
            handle.write(generated)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        return storage_secret(data_directory)
    return generated


def remove_storage_secret(data_directory: Path) -> bool:
    path = data_directory / STORAGE_SECRET_FILENAME
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".tmp").unlink(missing_ok=True)
    return not path.exists()
