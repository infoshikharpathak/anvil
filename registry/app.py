"""Anvil Registry — FastAPI service for policy storage, tool schema export,
and session trace collection / orphan detection."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from anvil.config import load_config

from .auth import ADMIN_KEY_ENV, AdminLoginRequired
from .routes import policies, tools, traces, ui
from .storage import RegistryStorage

CONFIG_PATH = os.environ.get("ANVIL_CONFIG", "anvil.yaml")
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Anvil Registry", version="0.1.0")

_config = load_config(CONFIG_PATH)
storage = RegistryStorage(
    _config,
    session_ttl=_config.settings.session_ttl,
    admin_key_digest=os.environ.get(ADMIN_KEY_ENV) or None,
)

app.state.storage = storage

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(policies.router)
app.include_router(tools.router)
app.include_router(traces.router)
app.include_router(ui.login_router)
app.include_router(ui.router)


@app.exception_handler(AdminLoginRequired)
def _admin_login_required(request: Request, exc: AdminLoginRequired):
    return ui.login_page(request, status_code=401)


@app.get("/health")
def health() -> dict:
    current = app.state.storage
    if current.keys.enabled:  # don't list policy names to unauthenticated callers
        return {"status": "ok"}
    return {"status": "ok", "policies": list(current.list_policies().keys())}
