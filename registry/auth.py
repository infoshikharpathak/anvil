"""Per-app API keys for the registry.

Each app may list ``api_keys: {client_id: sha256_hex}`` in config. A caller
sends its existing ``X-Client-Id`` header plus ``Authorization: Bearer <key>``;
the registry hashes the key, checks it against that client_id's stored
digest, and from then on serves only that app's policies, tools and traces.

If no app has any keys, auth is off and the registry behaves as before
(every caller sees everything).
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Header, HTTPException, Request

from anvil.models import AppConfig


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyTable:
    def __init__(self, apps: dict[str, AppConfig]) -> None:
        # client_id -> (app, digest)
        self._keys = {
            client_id: (app_name, digest)
            for app_name, app in apps.items()
            for client_id, digest in app.api_keys.items()
        }

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    def authenticate(self, client_id: str | None, key: str | None) -> str | None:
        """App for a valid (client_id, key) pair, else None."""
        if not client_id or not key or client_id not in self._keys:
            # Still hash, so unknown client IDs take about as long as bad keys.
            hmac.compare_digest(hash_api_key(key or ""), "0" * 64)
            return None
        app, digest = self._keys[client_id]
        return app if hmac.compare_digest(hash_api_key(key), digest) else None


def caller_app(
    request: Request,
    x_client_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str | None:
    """FastAPI dependency: the authenticated caller's app, or None when the
    registry has no keys configured (open mode)."""
    keys: KeyTable = request.app.state.storage.keys
    if not keys.enabled:
        return None
    key = None
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    app = keys.authenticate(x_client_id, key)
    if app is None:
        raise HTTPException(
            status_code=401,
            detail="Valid X-Client-Id and Authorization: Bearer <api key> required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return app
