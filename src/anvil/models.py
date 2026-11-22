"""Pydantic/dataclass models shared across the Anvil library."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .namespaces import DEFAULT_APP, LLM_SEP, canonical, qualify, split_id


class ViolationType(str, Enum):
    UNKNOWN_AGENT = "unknown_agent"
    DISALLOWED_TOOL = "disallowed_tool"
    MISSING_PREREQUISITE = "missing_prerequisite"
    INJECTION_DETECTED_REQUEST = "injection_detected_request"
    INJECTION_DETECTED_RESPONSE = "injection_detected_response"
    SCHEMA_VIOLATION = "schema_violation"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    INVALID_PARAMS = "invalid_params"
    DISALLOWED_DELEGATION = "disallowed_delegation"
    CROSS_APP_DENIED = "cross_app_denied"
    INJECTION_DETECTED_AGENT_INPUT = "injection_detected_agent_input"
    INJECTION_DETECTED_AGENT_OUTPUT = "injection_detected_agent_output"
    INJECTION_DETECTED_HANDOFF = "injection_detected_handoff"


class CorrectionInfo(BaseModel):
    violation: ViolationType
    message: str
    required_actions: list[str] = Field(default_factory=list)
    call_history: list[str] = Field(default_factory=list)


class ToolResponse(BaseModel):
    success: bool
    data: dict[str, Any] | list[Any] | None = None
    tool_executed: bool = False
    correction: CorrectionInfo | None = None
    # When a response was withheld (injection in it, or a strict schema
    # failure), the tool's raw result for the HOST only: excluded from
    # model_dump/JSON, so it never reaches the model. Lets the host tell the
    # user what was created (e.g. a transfer ID) without showing the text.
    withheld_data: Any = Field(default=None, exclude=True)


class AgentCheckResult(BaseModel):
    """Result of check_agent_input / check_agent_output / check_handoff.
    Like ToolResponse, a block is returned as data, never raised."""

    allowed: bool
    correction: CorrectionInfo | None = None
    # Output checks: which check decided (otp_guard, regex, or the classifier
    # label such as ai_instruction), and whether the content was blocked or
    # delivered but flagged (unrequested_action). flagged implies allowed.
    check: str | None = None
    label: str | None = None
    flagged: bool = False
    reason: str | None = None


# --------------------------------------------------------------------------
# Config models (anvil.yaml)
# --------------------------------------------------------------------------

Trust = Literal["trusted", "untrusted"]
Strictness = Literal["strict", "loose"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
InjectionScanMode = Literal["regex", "llm", "hybrid"]


class InjectionScannerConfig(BaseModel):
    """LLM-based injection scanner configuration.

    provider: 'openai' or 'anthropic' — determines API format.
    model: cheap/fast model to use (e.g. 'gpt-4o-mini', 'claude-haiku-3-5-20241022').
    api_key_env: environment variable name holding the API key.
    timeout: seconds before falling back to regex.
    """

    provider: Literal["openai", "anthropic"] = "openai"
    model: str = "gpt-4o-mini"
    api_key_env: str = "OPENAI_API_KEY"
    timeout: float = 5.0


class ResponseSchema(BaseModel):
    required_fields: list[str] = Field(default_factory=list)


class ToolConfig(BaseModel):
    name: str = ""  # canonical "app.tool" ID, filled in from the mapping key
    app: str = DEFAULT_APP
    # Exactly one of endpoint (full URL) or path (joined to the app's
    # base_url at load time) must be set. After loading, endpoint is always
    # the resolved URL and path is cleared.
    endpoint: str | None = None
    path: str | None = None
    method: HttpMethod = "POST"
    description: str = ""
    # The tool's own trust; an agent's trust_overrides take priority. None:
    # use settings.default_trust.
    trust: Trust | None = None
    timeout: float = 10.0
    injection_scanning: bool = True
    parameters: dict[str, Any] | None = None
    response_schema: ResponseSchema | None = None
    # Response fields to strip before anyone downstream sees them: the agent,
    # injection scans, schema checks, traces. Matched by key name at any
    # depth. For secrets a tool returns but the agent must never hold (an
    # OTP, a token).
    redact_fields: list[str] = Field(default_factory=list)


class AppAuth(BaseModel):
    """How Anvil authenticates to an app's tools. The secret itself never
    goes in config: only the name of the env var that holds it."""

    header: str
    secret_env: str
    prefix: str = ""  # e.g. "Bearer "


class AppConfig(BaseModel):
    name: str = ""  # filled in from the mapping key
    base_url: str | None = None
    auth: AppAuth | None = None
    # Registry API keys for this app: client_id -> SHA-256 hex of the key.
    # Only the registry reads these; they are stripped from every response.
    api_keys: dict[str, str] = Field(default_factory=dict)
    # Tools declared under the app. Moved into AnvilConfig.tools (keyed by
    # canonical "app.tool" ID) at load time, so this is empty afterwards.
    tools: dict[str, ToolConfig] = Field(default_factory=dict)


class Grant(BaseModel):
    """Lets other apps use one app's tool, or delegate to one app's agent.

        grants:
          - tool: notify.send_email
            to_apps: [ironclad]
          - agent: notify.dispatcher
            to_apps: [ironclad]

    Bare names refer to the default app.
    """

    tool: str | None = None
    agent: str | None = None
    to_apps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_target(self) -> "Grant":
        if (self.tool is None) == (self.agent is None):
            raise ValueError("a grant needs exactly one of 'tool' or 'agent'")
        if self.tool is not None:
            self.tool = canonical(self.tool)
        if self.agent is not None:
            self.agent = canonical(self.agent)
        return self


class SequenceRule(BaseModel):
    """*before* may run only after every tool in *requires* (all of) and at
    least one tool in *requires_any* (any of) ran earlier in the session."""

    before: str
    requires: list[str] = Field(default_factory=list)
    requires_any: list[str] = Field(default_factory=list)
    match_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_a_prerequisite(self) -> SequenceRule:
        if not self.requires and not self.requires_any:
            raise ValueError(f"sequence rule for '{self.before}' needs 'requires' or 'requires_any'")
        return self


class AgentPolicy(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    # Agents this agent may hand work off to. Enforced like allowed_tools:
    # a hard block regardless of strictness.
    allowed_delegations: list[str] = Field(default_factory=list)
    sequences: list[SequenceRule] = Field(default_factory=list)
    strictness: Strictness = "strict"
    trust_overrides: dict[str, Trust] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    description: str = ""
    # Keyed by agent ID: "app.agent", or a bare name for the default app.
    agents: dict[str, AgentPolicy] = Field(default_factory=dict)

    @field_validator("agents")
    @classmethod
    def _canonical_agent_ids(cls, agents: dict[str, AgentPolicy]) -> dict[str, AgentPolicy]:
        out: dict[str, AgentPolicy] = {}
        for key, agent in agents.items():
            if LLM_SEP in key:
                raise ValueError(f"agent '{key}' must not contain '{LLM_SEP}'")
            agent_id = canonical(key)
            if agent_id in out:
                raise ValueError(f"agent '{agent_id}' is declared more than once")
            out[agent_id] = agent
        return out


class Settings(BaseModel):
    injection_scanning: bool = True
    injection_scan_mode: InjectionScanMode = "regex"
    injection_scanner: InjectionScannerConfig | None = None
    default_strictness: Strictness = "strict"
    default_trust: Trust = "untrusted"
    session_ttl: int = 3600
    trace_fallback_dir: str = "./anvil_traces"


class AnvilConfig(BaseModel):
    apps: dict[str, AppConfig] = Field(default_factory=dict)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    policies: dict[str, PolicyConfig] = Field(default_factory=dict)
    grants: list[Grant] = Field(default_factory=list)
    settings: Settings = Field(default_factory=Settings)

    @model_validator(mode="after")
    def _flatten_apps(self) -> "AnvilConfig":
        """Give every tool a canonical "app.tool" key and a resolved endpoint.

        Top-level tools keep working as before: a bare key is the default
        app. Re-validating an already-flattened config is a no-op, which
        matters because the registry round-trips configs through JSON."""
        errors: list[str] = []
        for app_name, app in self.apps.items():
            app.name = app_name
            if "." in app_name or LLM_SEP in app_name:
                errors.append(f"app name '{app_name}' must not contain '.' or '{LLM_SEP}'")

        flat: dict[str, ToolConfig] = {}

        def add(key: str, cfg: ToolConfig) -> None:
            if key in flat:
                errors.append(f"tool '{key}' is declared more than once")
            cfg.name = key
            flat[key] = cfg

        for key, cfg in self.tools.items():
            app_name, short = split_id(key)
            if "." in key and cfg.app not in (DEFAULT_APP, app_name):
                errors.append(f"tool '{key}' has app '{cfg.app}' but its key says '{app_name}'")
            if LLM_SEP in short:
                errors.append(f"tool '{key}' must not contain '{LLM_SEP}'")
            cfg.app = app_name
            add(qualify(app_name, short), cfg)

        for app_name, app in self.apps.items():
            for short, cfg in app.tools.items():
                if "." in short or LLM_SEP in short:
                    errors.append(
                        f"tool '{short}' under app '{app_name}' must not contain '.' or '{LLM_SEP}'"
                    )
                cfg.app = app_name
                add(qualify(app_name, short), cfg)
            app.tools = {}

        for key, cfg in flat.items():
            app = self.apps.get(cfg.app)
            if cfg.app != DEFAULT_APP and app is None:
                errors.append(f"tool '{key}' belongs to undeclared app '{cfg.app}'")
                continue
            if cfg.path is not None:
                if cfg.endpoint is not None:
                    errors.append(f"tool '{key}' sets both endpoint and path; use one")
                elif app is None or not app.base_url:
                    errors.append(f"tool '{key}' uses path but app '{cfg.app}' has no base_url")
                else:
                    cfg.endpoint = app.base_url.rstrip("/") + "/" + cfg.path.lstrip("/")
                    cfg.path = None
            elif cfg.endpoint is None:
                errors.append(f"tool '{key}' needs an endpoint or a path")

        if errors:
            raise ValueError("; ".join(errors))
        self.tools = flat
        return self


# --------------------------------------------------------------------------
# Call log / trace models
# --------------------------------------------------------------------------

class CallResult(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    RESPONSE_WITHHELD = "response_withheld"


RecordKind = Literal["tool", "agent_input", "agent_output", "handoff"]


class RecordedCall(BaseModel):
    """One tool invocation (or agent check) as recorded in the per-session
    call log. Only kind == "tool" records count toward prerequisites and
    call history; agent checks are kept for the trace."""

    tool: str
    kind: RecordKind = "tool"
    target_agent: str | None = None
    agent: str
    params: dict[str, Any] = Field(default_factory=dict)
    result: CallResult
    violation: ViolationType | None = None
    correction_message: str | None = None
    latency_ms: float | None = None
    trace_id: str | None = None
    # The upstream's HTTP status when it answered with an error (4xx/5xx).
    http_status: int | None = None
    timestamp: str = Field(default_factory=lambda: _iso_now())


def _iso_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> float:
    return time.monotonic() * 1000
