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
    AgentCheckResult,
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
from .namespaces import DEFAULT_APP, app_of, canonical, resolve
from .policy import PolicyEngine
from .registry_client import fetch_policy_bundle
from .trace import TraceBuffer
from .validators import Classifier, scan_for_injection_async, validate_schema


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
        classifier: Classifier | None = None,
    ) -> None:
        """Two ways to construct:

        - `Anvil(config, policy)` — config already loaded (used by
          `from_config` and by tests). No network I/O.
        - `Anvil(registry_url=..., policy=...)` — fetches the policy bundle
          from the registry synchronously (one blocking call, startup only).
          If the registry is unreachable, raises AnvilError immediately
          rather than starting with no policy.

        `classifier` is an optional async ``(text) -> InjectionResult``
        callable that replaces the built-in httpx LLM injection scanner.
        """
        policy_name = policy
        if policy_name is None:
            raise AnvilError("`policy` is required.")

        self.session_id = session_id or new_session_id()
        self.registry_url = registry_url
        self.client_id = client_id
        self.classifier = classifier

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
        self.policy_engine = PolicyEngine(
            config.policies[policy_name], self.call_log, grants=config.grants
        )
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
        classifier: Classifier | None = None,
    ) -> "Anvil":
        config = load_config(path)
        return cls(config, policy, session_id=session_id, classifier=classifier)

    @classmethod
    async def from_registry(
        cls,
        registry_url: str,
        policy: str,
        session_id: str | None = None,
        client_id: str | None = None,
        classifier: Classifier | None = None,
    ) -> "Anvil":
        """Async variant of the registry-backed constructor, for callers
        already inside an event loop (avoids the sync client's blocking
        call)."""
        sid = session_id or new_session_id()
        config = await fetch_policy_bundle(registry_url, policy, sid, client_id)
        return cls(
            config,
            policy,
            session_id=sid,
            registry_url=registry_url,
            client_id=client_id,
            classifier=classifier,
        )

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
        """Register a tool at runtime. `name` may be "app.tool"; a bare name
        belongs to the default app."""
        tool_id = canonical(name)
        self._extra_tools[tool_id] = ToolConfig(
            name=tool_id,
            app=app_of(tool_id),
            endpoint=endpoint,
            method=method,
            trust=trust,
            timeout=timeout,
            **kwargs,
        )

    def tool(self, name: str, endpoint: str, method: str = "POST", **kwargs: Any) -> Callable:
        """Decorator form. The wrapped function body is not executed for the
        HTTP call itself (Anvil performs that based on `endpoint`); the
        decorator just registers the tool and returns a callable that runs
        it through `execute()`."""
        self.register_tool(name, endpoint, method=method, **kwargs)

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapper(agent_id: str, *, trace_id: str | None = None, **params: Any) -> ToolResponse:
                return await self.execute(agent_id=agent_id, tool_name=name, params=params, trace_id=trace_id)

            return wrapper

        return decorator

    def _agent_id(self, agent_id: str) -> str:
        """Canonical agent ID. A bare name belongs to the default app."""
        return canonical(agent_id)

    def _resolve_tool(self, tool_name: str, agent_id: str | None = None) -> ToolConfig:
        """Look up a tool. A bare name resolves within the calling agent's
        app, so an ironclad agent can say "get_accounts"."""
        ctx_app = app_of(agent_id) if agent_id else DEFAULT_APP
        tool_name = resolve(tool_name, ctx_app)
        cfg = self._extra_tools.get(tool_name) or self.config.tools.get(tool_name)
        if cfg is None:
            raise AnvilError(f"Tool '{tool_name}' is not registered.")
        return cfg

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        agent_id: str,
        tool_name: str,
        params: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> ToolResponse:
        """Run one tool call through policy, the real tool, and response checks.

        `trace_id` is an optional caller-supplied correlation id (e.g. from an
        orchestrator's request trace). It is stored on the recorded call, sent
        with the session trace, and included in audit log lines.
        """
        agent_id = self._agent_id(agent_id)
        tool_cfg = self._resolve_tool(tool_name, agent_id)
        tool_name = tool_cfg.name  # canonical "app.tool" ID from here on

        # --- Request path -------------------------------------------------
        decision = self.policy_engine.check_request(self.session_id, agent_id, tool_name, params)

        if not decision.allowed:
            assert decision.correction is not None
            # Hard blocks (unknown_agent, disallowed_tool) are always
            # enforced. Sequence violations respect the agent's strictness.
            hard_block = decision.correction.violation in (
                ViolationType.UNKNOWN_AGENT,
                ViolationType.CROSS_APP_DENIED,
                ViolationType.DISALLOWED_TOOL,
            )
            if hard_block or decision.enforce:
                self._record(agent_id, tool_name, params, CallResult.BLOCKED, decision.correction, trace_id=trace_id)
                return ToolResponse(success=False, data=None, tool_executed=False, correction=decision.correction)
            # loose strictness: log and fall through to execute anyway
            log_call(
                session_id=self.session_id,
                agent_id=agent_id,
                tool_name=tool_name,
                result="allowed_with_warning",
                violation=decision.correction.violation.value,
                enforced=False,
                trace_id=trace_id,
            )

        # Request-path injection scan (on the outgoing params)
        settings = self.config.settings
        if settings.injection_scanning and tool_cfg.injection_scanning:
            req_scan = await scan_for_injection_async(params, settings, classifier=self.classifier)
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
                self._record(agent_id, tool_name, params, CallResult.BLOCKED, correction, trace_id=trace_id)
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
            self._record(agent_id, tool_name, params, CallResult.BLOCKED, correction, trace_id=trace_id)
            return ToolResponse(success=False, data=None, tool_executed=False, correction=correction)
        except _UpstreamError as e:
            correction = CorrectionInfo(
                violation=ViolationType.UPSTREAM_ERROR,
                message=f"'{tool_name}' failed upstream: {e}. Not recorded as a prerequisite.",
                required_actions=[],
                call_history=self.call_log.history_names(self.session_id, agent_id),
            )
            self._record(agent_id, tool_name, params, CallResult.BLOCKED, correction, trace_id=trace_id)
            return ToolResponse(success=False, data=None, tool_executed=False, correction=correction)

        latency_ms = now_ms() - start

        # --- Response path (only for untrusted tools) -----------------------
        if trust == "untrusted":
            if settings.injection_scanning and tool_cfg.injection_scanning:
                resp_scan = await scan_for_injection_async(raw_data, settings, classifier=self.classifier)
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
                        agent_id, tool_name, params, CallResult.RESPONSE_WITHHELD, correction, latency_ms,
                        trace_id=trace_id,
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
                        agent_id, tool_name, params, CallResult.RESPONSE_WITHHELD, correction, latency_ms,
                        trace_id=trace_id,
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
                    trace_id=trace_id,
                )

        # --- Success ---------------------------------------------------------
        self._record(agent_id, tool_name, params, CallResult.SUCCESS, None, latency_ms, trace_id=trace_id)
        return ToolResponse(success=True, data=raw_data, tool_executed=True, correction=None)

    # ------------------------------------------------------------------
    # Agent checks (multi-agent pipelines)
    # ------------------------------------------------------------------

    async def check_agent_input(
        self, agent_id: str, content: Any, *, trace_id: str | None = None
    ) -> AgentCheckResult:
        """Check content about to be given to an agent: the agent must be in
        the policy, and the content must pass the injection scan."""
        agent_id = self._agent_id(agent_id)
        return await self._check_agent_content(
            agent_id, content, "agent_input", ViolationType.INJECTION_DETECTED_AGENT_INPUT,
            f"Input to agent '{agent_id}'", trace_id,
        )

    async def check_agent_output(
        self, agent_id: str, content: Any, *, trace_id: str | None = None
    ) -> AgentCheckResult:
        """Check content an agent produced before it is used downstream."""
        agent_id = self._agent_id(agent_id)
        return await self._check_agent_content(
            agent_id, content, "agent_output", ViolationType.INJECTION_DETECTED_AGENT_OUTPUT,
            f"Output from agent '{agent_id}'", trace_id,
        )

    async def check_handoff(
        self,
        from_agent: str,
        to_agent: str,
        payload: Any = None,
        *,
        trace_id: str | None = None,
    ) -> AgentCheckResult:
        """Check a delegation from one agent to another. `to_agent` must be in
        `from_agent`'s allowed_delegations (enforced like allowed_tools). If a
        payload is passed it is also injection-scanned. A bare `to_agent`
        resolves within `from_agent`'s app."""
        from_agent = self._agent_id(from_agent)
        to_agent = resolve(to_agent, app_of(from_agent))
        decision = self.policy_engine.check_handoff(self.session_id, from_agent, to_agent)
        if not decision.allowed:
            assert decision.correction is not None
            self._record_check(from_agent, "handoff", decision.correction, trace_id, to_agent)
            return AgentCheckResult(allowed=False, correction=decision.correction)

        if payload is not None:
            blocked = await self._scan_check_content(
                from_agent, payload, ViolationType.INJECTION_DETECTED_HANDOFF,
                f"Handoff from '{from_agent}' to '{to_agent}'",
            )
            if blocked is not None:
                self._record_check(from_agent, "handoff", blocked, trace_id, to_agent)
                return AgentCheckResult(allowed=False, correction=blocked)

        self._record_check(from_agent, "handoff", None, trace_id, to_agent)
        return AgentCheckResult(allowed=True)

    async def _check_agent_content(
        self,
        agent_id: str,
        content: Any,
        kind: str,
        violation: ViolationType,
        label: str,
        trace_id: str | None,
    ) -> AgentCheckResult:
        decision = self.policy_engine.check_agent_known(agent_id)
        if not decision.allowed:
            assert decision.correction is not None
            self._record_check(agent_id, kind, decision.correction, trace_id)
            return AgentCheckResult(allowed=False, correction=decision.correction)

        blocked = await self._scan_check_content(agent_id, content, violation, label)
        self._record_check(agent_id, kind, blocked, trace_id)
        if blocked is not None:
            return AgentCheckResult(allowed=False, correction=blocked)
        return AgentCheckResult(allowed=True)

    async def _scan_check_content(
        self, agent_id: str, content: Any, violation: ViolationType, label: str
    ) -> CorrectionInfo | None:
        settings = self.config.settings
        if not settings.injection_scanning:
            return None
        scan = await scan_for_injection_async(content, settings, classifier=self.classifier)
        if not scan.detected:
            return None
        return CorrectionInfo(
            violation=violation,
            message=(
                f"{label} contained suspicious instruction patterns "
                f"('{scan.matched_pattern}') and was blocked."
            ),
            required_actions=[],
            call_history=self.call_log.history_names(self.session_id, agent_id),
        )

    def _record_check(
        self,
        agent_id: str,
        kind: str,
        correction: CorrectionInfo | None,
        trace_id: str | None,
        target_agent: str | None = None,
    ) -> None:
        result = CallResult.BLOCKED if correction else CallResult.SUCCESS
        call = RecordedCall(
            tool=kind,
            kind=kind,
            target_agent=target_agent,
            agent=agent_id,
            result=result,
            violation=correction.violation if correction else None,
            correction_message=correction.message if correction else None,
            trace_id=trace_id,
        )
        self.call_log.record(self.session_id, call)
        log_call(
            session_id=self.session_id,
            agent_id=agent_id,
            tool_name=kind if target_agent is None else f"{kind}->{target_agent}",
            result=result.value,
            violation=correction.violation.value if correction else None,
            trace_id=trace_id,
        )

    async def _call_upstream(self, tool_cfg: ToolConfig, params: dict[str, Any]) -> dict[str, Any]:
        method = tool_cfg.method.upper()
        # Interpolate {placeholder} tokens in the endpoint URL from params,
        # removing consumed keys so they aren't sent again as query/body args.
        url, remaining = _interpolate_endpoint(tool_cfg.endpoint, params)
        try:
            async with httpx.AsyncClient(timeout=tool_cfg.timeout) as client:
                if method in ("GET", "DELETE"):
                    for k, v in remaining.items():
                        if isinstance(v, (dict, list)):
                            raise AnvilError(
                                f"GET/DELETE tool '{tool_cfg.name}' received a nested "
                                f"object for param '{k}'; only scalar query params are supported."
                            )
                    resp = await client.request(method, url, params=remaining)
                else:
                    resp = await client.request(method, url, json=remaining)
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
        trace_id: str | None = None,
    ) -> None:
        call = RecordedCall(
            tool=tool_name,
            agent=agent_id,
            params=params,
            result=result,
            violation=correction.violation if correction else None,
            correction_message=correction.message if correction else None,
            latency_ms=latency_ms,
            trace_id=trace_id,
        )
        self.call_log.record(self.session_id, call)
        log_call(
            session_id=self.session_id,
            agent_id=agent_id,
            tool_name=tool_name,
            result=result.value,
            violation=correction.violation.value if correction else None,
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def end_session(self) -> bool:
        """Flush the session trace to the registry. Returns True if the
        registry accepted it; False if it fell back to a local file."""
        return await self.trace.flush(self.registry_url)


def _interpolate_endpoint(endpoint: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Replace ``{key}`` placeholders in an endpoint URL with matching param
    values.  Returns ``(resolved_url, remaining_params)`` — consumed keys are
    removed from the params dict so they don't get sent again as query/body
    arguments."""
    import re

    remaining = dict(params)
    used: set[str] = set()

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key in remaining:
            used.add(key)
            return str(remaining[key])
        return m.group(0)  # leave unresolved placeholders as-is

    url = re.sub(r"\{(\w+)\}", _replace, endpoint)
    for key in used:
        remaining.pop(key, None)
    return url, remaining


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
