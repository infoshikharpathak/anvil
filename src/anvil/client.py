"""Anvil — the in-process library entry point.

Wraps tool calls: checks policy before the real call, forwards to the real
tool, then validates/scans the response. Every outcome is returned as a
normal ToolResponse — never an exception — so agent frameworks that raise on
exceptions never see the pipeline crash.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

import httpx

from .audit import log_call
from .call_log import CallLog
from .config import load_config
from .models import (
    AnvilConfig,
    CallResult,
    CorrectionInfo,
    RecordedCall,
    ToolConfig,
    ToolResponse,
    ViolationType,
    new_session_id,
    now_ms,
)
from .policy import PolicyEngine
from .registry_client import fetch_policy_bundle
from .trace import TraceBuffer
from .validators import scan_for_injection, validate_schema


class AnvilError(Exception):
    """Raised for Anvil's own failures (bad config, unregistered tool in the
    wrapper) — never for policy violations, which come back as ToolResponse."""


class Anvil:
    def __init__(
        self,
        config: AnvilConfig | None = None,
        policy: str | None = None,
        *,
        registry_url: str | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        """Two ways to construct:

        - `Anvil(config, policy)` — config already loaded (used by
          `from_config` and by tests). No network I/O.
        - `Anvil(registry_url=..., policy=...)` — fetches the policy bundle
          from the registry synchronously (one blocking call, startup only).
          If the registry is unreachable, raises AnvilError immediately
          rather than starting with no policy.
        """
        policy_name = policy
        if policy_name is None:
            raise AnvilError("`policy` is required.")

        self.session_id = session_id or new_session_id()
        self.registry_url = registry_url
        self.client_id = client_id

        if config is None:
            if registry_url is None:
                raise AnvilError("Either `config` or `registry_url` must be provided.")
            config = _fetch_policy_bundle_sync(registry_url, policy_name, self.session_id, client_id)
        assert config is not None

        if policy_name not in config.policies:
            raise AnvilError(f"Policy '{policy_name}' not found in config.")

        self.config = config
        self.policy_name = policy_name

        self.call_log = CallLog(session_ttl=config.settings.session_ttl)
        self.policy_engine = PolicyEngine(config.policies[policy_name], self.call_log)
        self.trace = TraceBuffer(
            self.session_id,
            policy_name,
            self.call_log,
            fallback_dir=config.settings.trace_fallback_dir,
        )
        self._extra_tools: dict[str, ToolConfig] = {}

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        path: str,
        policy: str,
        session_id: str | None = None,
    ) -> "Anvil":
        config = load_config(path)
        return cls(config, policy, session_id=session_id)

    @classmethod
    async def from_registry(
        cls,
        registry_url: str,
        policy: str,
        session_id: str | None = None,
        client_id: str | None = None,
    ) -> "Anvil":
        """Async variant of the registry-backed constructor, for callers
        already inside an event loop (avoids the sync client's blocking
        call)."""
        sid = session_id or new_session_id()
        config = await fetch_policy_bundle(registry_url, policy, sid, client_id)
        return cls(config, policy, session_id=sid, registry_url=registry_url, client_id=client_id)

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def register_tool(
        self,
        name: str,
        endpoint: str,
        method: str = "POST",
        trust: str = "untrusted",
        timeout: float = 10.0,
        **kwargs: Any,
    ) -> None:
        self._extra_tools[name] = ToolConfig(
            name=name, endpoint=endpoint, method=method, trust=trust, timeout=timeout, **kwargs
        )

    def tool(self, name: str, endpoint: str, method: str = "POST", **kwargs: Any) -> Callable:
        """Decorator form. The wrapped function body is not executed for the
        HTTP call itself (Anvil performs that based on `endpoint`); the
        decorator just registers the tool and returns a callable that runs
        it through `execute()`."""
        self.register_tool(name, endpoint, method=method, **kwargs)

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapper(agent_id: str, **params: Any) -> ToolResponse:
                return await self.execute(agent_id=agent_id, tool_name=name, params=params)

            return wrapper

        return decorator

    def _resolve_tool(self, tool_name: str) -> ToolConfig:
        cfg = self._extra_tools.get(tool_name) or self.config.tools.get(tool_name)
        if cfg is None:
            raise AnvilError(f"Tool '{tool_name}' is not registered.")
        return cfg

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def execute(self, agent_id: str, tool_name: str, params: dict[str, Any]) -> ToolResponse:
        tool_cfg = self._resolve_tool(tool_name)

        # --- Request path -------------------------------------------------
        decision = self.policy_engine.check_request(self.session_id, agent_id, tool_name, params)

        if not decision.allowed:
            assert decision.correction is not None
            # Hard blocks (unknown_agent, disallowed_tool) are always
            # enforced. Sequence violations respect the agent's strictness.
            hard_block = decision.correction.violation in (
                ViolationType.UNKNOWN_AGENT,
                ViolationType.DISALLOWED_TOOL,
            )
            if hard_block or decision.enforce:
                self._record(agent_id, tool_name, params, CallResult.BLOCKED, decision.correction)
                return ToolResponse(success=False, data=None, tool_executed=False, correction=decision.correction)
            # loose strictness: log and fall through to execute anyway
            log_call(
                session_id=self.session_id,
                agent_id=agent_id,
                tool_name=tool_name,
                result="allowed_with_warning",
                violation=decision.correction.violation.value,
                enforced=False,
            )

        # Request-path injection scan (on the outgoing params)
        settings = self.config.settings
        if settings.injection_scanning and tool_cfg.injection_scanning:
            req_scan = scan_for_injection(params)
            if req_scan.detected:
                correction = CorrectionInfo(
                    violation=ViolationType.INJECTION_DETECTED_REQUEST,
                    message=(
                        f"Request to '{tool_name}' contained suspicious instruction "
                        f"patterns ('{req_scan.matched_pattern}') and was blocked before execution."
                    ),
                    required_actions=[],
                    call_history=self.call_log.history_names(self.session_id, agent_id),
                )
                self._record(agent_id, tool_name, params, CallResult.BLOCKED, correction)
                return ToolResponse(success=False, data=None, tool_executed=False, correction=correction)

        # --- Forward to the real tool --------------------------------------
        trust = self.policy_engine.resolve_trust(agent_id, tool_name, settings.default_trust)
        start = now_ms()
        try:
            raw_data = await self._call_upstream(tool_cfg, params)
        except _UpstreamTimeout:
            correction = CorrectionInfo(
                violation=ViolationType.UPSTREAM_TIMEOUT,
                message=f"'{tool_name}' timed out after {tool_cfg.timeout}s. Not recorded as a prerequisite.",
                required_actions=[],
                call_history=self.call_log.history_names(self.session_id, agent_id),
            )
            self._record(agent_id, tool_name, params, CallResult.BLOCKED, correction)
            return ToolResponse(success=False, data=None, tool_executed=False, correction=correction)
        except _UpstreamError as e:
            correction = CorrectionInfo(
                violation=ViolationType.UPSTREAM_ERROR,
                message=f"'{tool_name}' failed upstream: {e}. Not recorded as a prerequisite.",
                required_actions=[],
                call_history=self.call_log.history_names(self.session_id, agent_id),
            )
            self._record(agent_id, tool_name, params, CallResult.BLOCKED, correction)
            return ToolResponse(success=False, data=None, tool_executed=False, correction=correction)

        latency_ms = now_ms() - start

        # --- Response path (only for untrusted tools) -----------------------
        if trust == "untrusted":
            if settings.injection_scanning and tool_cfg.injection_scanning:
                resp_scan = scan_for_injection(raw_data)
                if resp_scan.detected:
                    correction = CorrectionInfo(
                        violation=ViolationType.INJECTION_DETECTED_RESPONSE,
                        message=(
                            f"'{tool_name}' was executed successfully, but the tool response was "
                            f"withheld because suspicious instruction patterns "
                            f"('{resp_scan.matched_pattern}') were detected. "
                            f"Do NOT retry this call — the action was already committed."
                        ),
                        required_actions=[],
                        call_history=self.call_log.history_names(self.session_id, agent_id),
                    )
                    self._record(
                        agent_id, tool_name, params, CallResult.RESPONSE_WITHHELD, correction, latency_ms
                    )
                    return ToolResponse(success=False, data=None, tool_executed=True, correction=correction)

            schema_result = validate_schema(raw_data, tool_cfg.response_schema)
            if not schema_result.valid:
                strictness = self.policy_engine.strictness_for(agent_id, settings.default_strictness)
                correction = CorrectionInfo(
                    violation=ViolationType.SCHEMA_VIOLATION,
                    message=(
                        f"'{tool_name}' response is missing required fields: {schema_result.missing_fields}."
                    ),
                    required_actions=[],
                    call_history=self.call_log.history_names(self.session_id, agent_id),
                )
                if strictness == "strict":
                    self._record(
                        agent_id, tool_name, params, CallResult.RESPONSE_WITHHELD, correction, latency_ms
                    )
                    return ToolResponse(success=False, data=None, tool_executed=True, correction=correction)
                # loose: log warning, pass response through anyway
                log_call(
                    session_id=self.session_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    result="schema_violation_allowed",
                    violation=ViolationType.SCHEMA_VIOLATION.value,
                    enforced=False,
                )

        # --- Success ---------------------------------------------------------
        self._record(agent_id, tool_name, params, CallResult.SUCCESS, None, latency_ms)
        return ToolResponse(success=True, data=raw_data, tool_executed=True, correction=None)

    async def _call_upstream(self, tool_cfg: ToolConfig, params: dict[str, Any]) -> dict[str, Any]:
        method = tool_cfg.method.upper()
        try:
            async with httpx.AsyncClient(timeout=tool_cfg.timeout) as client:
                if method in ("GET", "DELETE"):
                    for k, v in params.items():
                        if isinstance(v, (dict, list)):
                            raise AnvilError(
                                f"GET/DELETE tool '{tool_cfg.name}' received a nested "
                                f"object for param '{k}'; only scalar query params are supported."
                            )
                    resp = await client.request(method, tool_cfg.endpoint, params=params)
                else:
                    resp = await client.request(method, tool_cfg.endpoint, json=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as e:
            raise _UpstreamTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise _UpstreamError(str(e)) from e

    def _record(
        self,
        agent_id: str,
        tool_name: str,
        params: dict[str, Any],
        result: CallResult,
        correction: CorrectionInfo | None,
        latency_ms: float | None = None,
    ) -> None:
        call = RecordedCall(
            tool=tool_name,
            agent=agent_id,
            params=params,
            result=result,
            violation=correction.violation if correction else None,
            correction_message=correction.message if correction else None,
            latency_ms=latency_ms,
        )
        self.call_log.record(self.session_id, call)
        log_call(
            session_id=self.session_id,
            agent_id=agent_id,
            tool_name=tool_name,
            result=result.value,
            violation=correction.violation.value if correction else None,
        )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def end_session(self) -> bool:
        """Flush the session trace to the registry. Returns True if the
        registry accepted it; False if it fell back to a local file."""
        return await self.trace.flush(self.registry_url)


class _UpstreamTimeout(Exception):
    pass


class _UpstreamError(Exception):
    pass


def _fetch_policy_bundle_sync(
    registry_url: str, policy_name: str, session_id: str, client_id: str | None
) -> AnvilConfig:
    """Blocking fetch used only from the synchronous `Anvil(...)` constructor
    at startup — the one network call the library makes before running
    independently of the registry."""
    headers = {"X-Session-Id": session_id}
    if client_id:
        headers["X-Client-Id"] = client_id
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{registry_url}/policies/{policy_name}", headers=headers)
            resp.raise_for_status()
            return AnvilConfig.model_validate(resp.json())
    except httpx.HTTPError as e:
        raise AnvilError(f"Could not fetch policy '{policy_name}' from registry '{registry_url}': {e}") from e
