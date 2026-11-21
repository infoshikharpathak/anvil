"""Per-app API keys for the registry.

Each app may list ``api_keys: {client_id: sha256_hex}`` in config. A caller
sends its existing ``X-Client-Id`` header plus ``Authorization: Bearer <key>``;
the registry hashes the key, checks it against that client_id's stored
digest, and from then on serves only that app's policies, tools and traces.

If no app has any keys, auth is off and the registry behaves as before
(every caller sees everything).

Admin: once app keys exist, the UI and unscoped API access need the admin
key, whose SHA-256 digest is set in the ANVIL_ADMIN_KEY env var. Scripts
send it as ``Authorization: Bearer <admin key>``. The browser UI logs in at
/ui/login and gets an HttpOnly session cookie (a random token, never the
key). The JSON API accepts that cookie only together with the
``X-Anvil-UI`` header the UI's fetch helpers send, so a cross-site form
can't ride on it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from fastapi import Header, HTTPException, Request

from anvil.models import AppConfig


ADMIN_KEY_ENV = "ANVIL_ADMIN_KEY"
SESSION_COOKIE = "anvil_admin_session"
UI_HEADER = "X-Anvil-UI"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AdminLoginRequired(Exception):
    """Raised by UI routes; app.py renders the login page with a 401."""


class KeyTable:
    def __init__(self, apps: dict[str, AppConfig], admin_digest: str | None = None) -> None:
        if admin_digest is not None and not _SHA256_HEX.fullmatch(admin_digest):
            raise ValueError(
                f"{ADMIN_KEY_ENV} must be the SHA-256 hex digest of the admin key, not the key itself"
            )
        self.admin_digest = admin_digest
        self._admin_sessions: set[str] = set()
        # client_id -> (app, digest)
        self._keys = {
            client_id: (app_name, digest)
            for app_name, app in apps.items()
            for client_id, digest in app.api_keys.items()
        }

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    def is_admin_key(self, key: str | None) -> bool:
        if not key or self.admin_digest is None:
            return False
        return hmac.compare_digest(hash_api_key(key), self.admin_digest)

    def new_admin_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._admin_sessions.add(token)
        return token

    def is_admin_session(self, token: str | None) -> bool:
        return bool(token) and any(hmac.compare_digest(token, t) for t in self._admin_sessions)

    def end_admin_session(self, token: str | None) -> None:
        self._admin_sessions.discard(token or "")

    def authenticate(self, client_id: str | None, key: str | None) -> str | None:
        """App for a valid (client_id, key) pair, else None."""
        if not client_id or not key or client_id not in self._keys:
            # Still hash, so unknown client IDs take about as long as bad keys.
            hmac.compare_digest(hash_api_key(key or ""), "0" * 64)
            return None
        app, digest = self._keys[client_id]
        return app if hmac.compare_digest(hash_api_key(key), digest) else None


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def is_admin(request: Request, *, require_ui_header: bool) -> bool:
    keys: KeyTable = request.app.state.storage.keys
    if keys.is_admin_key(_bearer(request.headers.get("authorization"))):
        return True
    if require_ui_header and request.headers.get(UI_HEADER) != "1":
        return False
    return keys.is_admin_session(request.cookies.get(SESSION_COOKIE))


def require_admin_ui(request: Request) -> None:
    """Dependency for UI pages: open when no app keys exist, else admin only."""
    if request.app.state.storage.keys.enabled and not is_admin(request, require_ui_header=False):
        raise AdminLoginRequired()


def caller_app(
    request: Request,
    x_client_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str | None:
    """FastAPI dependency: the authenticated caller's app, or None for an
    unscoped caller (open mode, or the admin)."""
    keys: KeyTable = request.app.state.storage.keys
    if not keys.enabled:
        return None
    if is_admin(request, require_ui_header=True):
        return None
    app = keys.authenticate(x_client_id, _bearer(authorization))
    if app is None:
        raise HTTPException(
            status_code=401,
            detail="Valid X-Client-Id and Authorization: Bearer <api key> required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return app
