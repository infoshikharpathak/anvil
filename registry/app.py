"""Anvil Registry — FastAPI service for policy storage, tool schema export,
and session trace collection / orphan detection."""

from __future__ import annotations

import os

from fastapi import FastAPI

from anvil.config import load_config

from .routes import policies, tools, traces
from .storage import RegistryStorage

CONFIG_PATH = os.environ.get("ANVIL_CONFIG", "anvil.yaml")

app = FastAPI(title="Anvil Registry", version="0.1.0")

_config = load_config(CONFIG_PATH)
storage = RegistryStorage(_config, session_ttl=_config.settings.session_ttl)

app.state.storage = storage

app.include_router(policies.router)
app.include_router(tools.router)
app.include_router(traces.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "policies": list(storage.list_policies().keys())}
