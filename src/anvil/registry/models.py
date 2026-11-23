"""Registry-specific models: policy checkouts, stored traces, orphan sessions."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, Field


class PolicyCheckout(BaseModel):
    session_id: str
    policy_name: str
    client_id: str | None = None
    app: str | None = None
    checked_out_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


class StoredTrace(BaseModel):
    session_id: str
    policy: str
    started_at: str
    ended_at: str
    calls: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    app: str | None = None
    client_id: str | None = None
    received_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


class OrphanSession(BaseModel):
    session_id: str
    policy_name: str
    client_id: str | None = None
    app: str | None = None
    checked_out_at: str
    seconds_since_checkout: float
