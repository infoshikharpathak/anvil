"""In-memory storage for the Anvil Registry (policies, traces, checkouts).

v1: no database. Everything lives in this process's memory and resets on
restart. Policies are seeded from anvil.yaml at startup; traces/checkouts
accumulate at runtime.
"""

from __future__ import annotations

import datetime
import time
from typing import Any

from anvil.models import AnvilConfig, PolicyConfig, ToolConfig

from .models import OrphanSession, PolicyCheckout, StoredTrace


class RegistryStorage:
    def __init__(self, config: AnvilConfig, session_ttl: int = 3600) -> None:
        self.tools: dict[str, ToolConfig] = dict(config.tools)
        self.policies: dict[str, PolicyConfig] = dict(config.policies)
        self.settings = config.settings
        self.session_ttl = session_ttl

        self._traces: dict[str, StoredTrace] = {}
        # session_id -> checkout (last checkout wins for orphan lookup)
        self._checkouts: dict[str, PolicyCheckout] = {}
        self._checkout_monotonic: dict[str, float] = {}

    # -- policies ---------------------------------------------------------

    def get_full_config_for_policy(self, policy_name: str) -> AnvilConfig | None:
        policy = self.policies.get(policy_name)
        if policy is None:
            return None
        return AnvilConfig(tools=self.tools, policies={policy_name: policy}, settings=self.settings)

    def list_policies(self) -> dict[str, PolicyConfig]:
        return self.policies

    def upsert_policy(self, name: str, policy: PolicyConfig) -> None:
        self.policies[name] = policy

    # -- tools --------------------------------------------------------------

    def list_tools(self) -> dict[str, ToolConfig]:
        return self.tools

    def get_tool(self, name: str) -> ToolConfig | None:
        return self.tools.get(name)

    # -- checkouts ------------------------------------------------------------

    def record_checkout(self, session_id: str, policy_name: str, client_id: str | None) -> None:
        self._checkouts[session_id] = PolicyCheckout(
            session_id=session_id, policy_name=policy_name, client_id=client_id
        )
        self._checkout_monotonic[session_id] = time.monotonic()

    # -- traces -----------------------------------------------------------

    def store_trace(self, trace: dict[str, Any]) -> None:
        stored = StoredTrace(**trace)
        self._traces[stored.session_id] = stored
        # a completed trace clears the checkout — no longer an orphan candidate
        self._checkouts.pop(trace["session_id"], None)
        self._checkout_monotonic.pop(trace["session_id"], None)

    def list_traces(self, limit: int = 50) -> list[StoredTrace]:
        traces = sorted(self._traces.values(), key=lambda t: t.received_at, reverse=True)
        return traces[:limit]

    def get_trace(self, session_id: str) -> StoredTrace | None:
        return self._traces.get(session_id)

    def list_checkouts(self) -> list[dict[str, Any]]:
        """All sessions that fetched a policy but haven't posted a trace yet
        (in-flight or, past session_ttl, likely orphaned). Used by the UI's
        activity feed to show sessions currently in progress."""
        now = time.monotonic()
        result = []
        for session_id, checkout in self._checkouts.items():
            elapsed = now - self._checkout_monotonic[session_id]
            result.append(
                {
                    "session_id": session_id,
                    "policy_name": checkout.policy_name,
                    "client_id": checkout.client_id,
                    "checked_out_at": checkout.checked_out_at,
                    "elapsed_seconds": round(elapsed, 1),
                    "status": "orphan" if elapsed > self.session_ttl else "active",
                }
            )
        result.sort(key=lambda c: c["checked_out_at"], reverse=True)
        return result

    def orphan_sessions(self) -> list[OrphanSession]:
        now = time.monotonic()
        orphans = []
        for session_id, checkout in self._checkouts.items():
            elapsed = now - self._checkout_monotonic[session_id]
            if elapsed > self.session_ttl:
                orphans.append(
                    OrphanSession(
                        session_id=session_id,
                        policy_name=checkout.policy_name,
                        client_id=checkout.client_id,
                        checked_out_at=checkout.checked_out_at,
                        seconds_since_checkout=elapsed,
                    )
                )
        return orphans
