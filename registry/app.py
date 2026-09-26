"""Anvil Registry — FastAPI service for policy storage, tool schema export,
and session trace collection / orphan detection."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from anvil.config import load_config

from .routes import policies, tools, traces, ui
from .storage import RegistryStorage

CONFIG_PATH = os.environ.get("ANVIL_CONFIG", "anvil.yaml")
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Anvil Registry", version="0.1.0")

_config = load_config(CONFIG_PATH)
storage = RegistryStorage(_config, session_ttl=_config.settings.session_ttl)

app.state.storage = storage

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(policies.router)
app.include_router(tools.router)
app.include_router(traces.router)
app.include_router(ui.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "policies": list(storage.list_policies().keys())}
