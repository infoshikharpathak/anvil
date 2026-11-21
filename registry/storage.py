"""In-memory storage for the Anvil Registry (policies, traces, checkouts).

v1: no database. Everything lives in this process's memory and resets on
restart. Policies are seeded from anvil.yaml at startup; traces/checkouts
accumulate at runtime.
"""

from __future__ import annotations

import datetime
import time
from typing import Any

from anvil.models import AnvilConfig, AppConfig, PolicyConfig, ToolConfig
from anvil.namespaces import app_of

from .auth import KeyTable
from .models import OrphanSession, PolicyCheckout, StoredTrace


def policy_owner(policy: PolicyConfig) -> str | None:
    """The app a policy belongs to: the single app all its agents share.
    Empty or mixed-app policies belong to no app, so app keys never see them."""
    apps = {app_of(a) for a in policy.agents}
    return apps.pop() if len(apps) == 1 else None


class RegistryStorage:
    def __init__(self, config: AnvilConfig, session_ttl: int = 3600) -> None:
        self.apps: dict[str, AppConfig] = dict(config.apps)
        self.tools: dict[str, ToolConfig] = dict(config.tools)
        self.grants = list(config.grants)
        self.keys = KeyTable(config.apps)
        self.policies: dict[str, PolicyConfig] = dict(config.policies)
        self.settings = config.settings
        self.session_ttl = session_ttl

        self._traces: dict[str, StoredTrace] = {}
        # session_id -> checkout (last checkout wins for orphan lookup)
        self._checkouts: dict[str, PolicyCheckout] = {}
        self._checkout_monotonic: dict[str, float] = {}

    # -- policies ---------------------------------------------------------

    # Every read takes the caller's app (None = open mode / admin: no
    # scoping). An app caller sees its own policies, its own tools plus
    # tools granted to it, and only the grants that name it.

    def owns(self, app: str | None, policy: PolicyConfig) -> bool:
        return app is None or policy_owner(policy) == app

    def get_full_config_for_policy(self, policy_name: str, app: str | None = None) -> AnvilConfig | None:
        policy = self.policies.get(policy_name)
        if policy is None or not self.owns(app, policy):
            return None
        tools = self.list_tools(app)
        if app is None:
            apps, grants = self.apps, self.grants
        else:
            wanted = {app} | {t.app for t in tools.values()}
            apps = {n: a for n, a in self.apps.items() if n in wanted}
            grants = [g.model_copy(update={"to_apps": [app]}) for g in self.grants if app in g.to_apps]
        return AnvilConfig(
            # API key digests never leave the registry.
            apps={n: a.model_copy(update={"api_keys": {}}) for n, a in apps.items()},
            tools=tools,
            policies={policy_name: policy},
            grants=grants,
            settings=self.settings,
        )

    def list_policies(self, app: str | None = None) -> dict[str, PolicyConfig]:
        return {n: p for n, p in self.policies.items() if self.owns(app, p)}

    def upsert_policy(self, name: str, policy: PolicyConfig) -> None:
        self.policies[name] = policy

    def delete_policy(self, name: str) -> None:
        self.policies.pop(name, None)

    # -- tools --------------------------------------------------------------

    def list_tools(self, app: str | None = None) -> dict[str, ToolConfig]:
        if app is None:
            return self.tools
        granted = {g.tool for g in self.grants if g.tool and app in g.to_apps}
        return {n: t for n, t in self.tools.items() if t.app == app or n in granted}

    def get_tool(self, name: str, app: str | None = None) -> ToolConfig | None:
        return self.list_tools(app).get(name)

    # -- checkouts ------------------------------------------------------------

    def record_checkout(
        self, session_id: str, policy_name: str, client_id: str | None, app: str | None = None
    ) -> None:
        self._checkouts[session_id] = PolicyCheckout(
            session_id=session_id, policy_name=policy_name, client_id=client_id, app=app
        )
        self._checkout_monotonic[session_id] = time.monotonic()

    # -- traces -----------------------------------------------------------

    def store_trace(
        self, trace: dict[str, Any], app: str | None = None, client_id: str | None = None
    ) -> None:
        stored = StoredTrace(**{**trace, "app": app, "client_id": client_id})
        self._traces[stored.session_id] = stored
        # a completed trace clears the checkout — no longer an orphan candidate
        self._checkouts.pop(trace["session_id"], None)
        self._checkout_monotonic.pop(trace["session_id"], None)

    def list_traces(self, limit: int = 50, app: str | None = None) -> list[StoredTrace]:
        traces = [t for t in self._traces.values() if app is None or t.app == app]
        traces.sort(key=lambda t: t.received_at, reverse=True)
        return traces[:limit]

    def get_trace(self, session_id: str, app: str | None = None) -> StoredTrace | None:
        trace = self._traces.get(session_id)
        if trace is None or (app is not None and trace.app != app):
            return None
        return trace

    def list_checkouts(self, app: str | None = None) -> list[dict[str, Any]]:
        """All sessions that fetched a policy but haven't posted a trace yet
        (in-flight or, past session_ttl, likely orphaned). Used by the UI's
        activity feed to show sessions currently in progress."""
        now = time.monotonic()
        result = []
        for session_id, checkout in self._checkouts.items():
            if app is not None and checkout.app != app:
                continue
            elapsed = now - self._checkout_monotonic[session_id]
            result.append(
                {
                    "session_id": session_id,
                    "policy_name": checkout.policy_name,
                    "client_id": checkout.client_id,
                    "app": checkout.app,
                    "checked_out_at": checkout.checked_out_at,
                    "elapsed_seconds": round(elapsed, 1),
                    "status": "orphan" if elapsed > self.session_ttl else "active",
                }
            )
        result.sort(key=lambda c: c["checked_out_at"], reverse=True)
        return result

    def orphan_sessions(self, app: str | None = None) -> list[OrphanSession]:
        now = time.monotonic()
        orphans = []
        for session_id, checkout in self._checkouts.items():
            if app is not None and checkout.app != app:
                continue
            elapsed = now - self._checkout_monotonic[session_id]
            if elapsed > self.session_ttl:
                orphans.append(
                    OrphanSession(
                        session_id=session_id,
                        policy_name=checkout.policy_name,
                        client_id=checkout.client_id,
                        app=checkout.app,
                        checked_out_at=checkout.checked_out_at,
                        seconds_since_checkout=elapsed,
                    )
                )
        return orphans
