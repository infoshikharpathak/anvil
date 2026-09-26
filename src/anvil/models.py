"""Pydantic/dataclass models shared across the Anvil library."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ViolationType(str, Enum):
    UNKNOWN_AGENT = "unknown_agent"
    DISALLOWED_TOOL = "disallowed_tool"
    MISSING_PREREQUISITE = "missing_prerequisite"
    INJECTION_DETECTED_REQUEST = "injection_detected_request"
    INJECTION_DETECTED_RESPONSE = "injection_detected_response"
    SCHEMA_VIOLATION = "schema_violation"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"


class CorrectionInfo(BaseModel):
    violation: ViolationType
    message: str
    required_actions: list[str] = Field(default_factory=list)
    call_history: list[str] = Field(default_factory=list)


class ToolResponse(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    tool_executed: bool = False
    correction: CorrectionInfo | None = None


# --------------------------------------------------------------------------
# Config models (anvil.yaml)
# --------------------------------------------------------------------------

Trust = Literal["trusted", "untrusted"]
Strictness = Literal["strict", "loose"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class ResponseSchema(BaseModel):
    required_fields: list[str] = Field(default_factory=list)


class ToolConfig(BaseModel):
    name: str = ""  # filled in by loader from the mapping key
    endpoint: str
    method: HttpMethod = "POST"
    description: str = ""
    trust: Trust = "untrusted"
    timeout: float = 10.0
    injection_scanning: bool = True
    parameters: dict[str, Any] | None = None
    response_schema: ResponseSchema | None = None


class SequenceRule(BaseModel):
    before: str
    requires: list[str]
    match_fields: list[str] = Field(default_factory=list)


class AgentPolicy(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list)
    sequences: list[SequenceRule] = Field(default_factory=list)
    strictness: Strictness = "strict"
    trust_overrides: dict[str, Trust] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    description: str = ""
    agents: dict[str, AgentPolicy] = Field(default_factory=dict)


class Settings(BaseModel):
    injection_scanning: bool = True
    default_strictness: Strictness = "strict"
    default_trust: Trust = "untrusted"
    session_ttl: int = 3600
    trace_fallback_dir: str = "./anvil_traces"


class AnvilConfig(BaseModel):
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    policies: dict[str, PolicyConfig] = Field(default_factory=dict)
    settings: Settings = Field(default_factory=Settings)

    @field_validator("tools")
    @classmethod
    def _stamp_names(cls, tools: dict[str, ToolConfig]) -> dict[str, ToolConfig]:
        for name, cfg in tools.items():
            cfg.name = name
        return tools


# --------------------------------------------------------------------------
# Call log / trace models
# --------------------------------------------------------------------------

class CallResult(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    RESPONSE_WITHHELD = "response_withheld"


class RecordedCall(BaseModel):
    """One tool invocation as recorded in the per-session call log."""

    tool: str
    agent: str
    params: dict[str, Any] = Field(default_factory=dict)
    result: CallResult
    violation: ViolationType | None = None
    correction_message: str | None = None
    latency_ms: float | None = None
    timestamp: str = Field(default_factory=lambda: _iso_now())


def _iso_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> float:
    return time.monotonic() * 1000
