"""PolicyEngine — evaluates the active policy for a given agent/tool/call."""

from __future__ import annotations

from dataclasses import dataclass

from .call_log import CallLog
from .models import AgentPolicy, CorrectionInfo, PolicyConfig, ViolationType


@dataclass
class PolicyDecision:
    allowed: bool
    correction: CorrectionInfo | None = None
    # When allowed is False but strictness is "loose" for a sequence/schema
    # violation, `enforce` is False: the caller should log and proceed anyway.
    enforce: bool = True


class PolicyEngine:
    def __init__(self, policy: PolicyConfig, call_log: CallLog) -> None:
        self.policy = policy
        self.call_log = call_log

    def agent_policy(self, agent_id: str) -> AgentPolicy | None:
        return self.policy.agents.get(agent_id)

    def check_request(
        self,
        session_id: str,
        agent_id: str,
        tool_name: str,
        params: dict,
    ) -> PolicyDecision:
        """Request-path check: allowed_tools + sequences. Always called before
        the tool is invoked."""
        agent = self.agent_policy(agent_id)

        # 1. unknown agent -> hard block, always enforced
        if agent is None:
            return PolicyDecision(
                allowed=False,
                correction=CorrectionInfo(
                    violation=ViolationType.UNKNOWN_AGENT,
                    message=f"Agent '{agent_id}' is not defined in the active policy.",
                    required_actions=[],
                    call_history=[],
                ),
            )

        # 2. disallowed tool -> hard block, always enforced regardless of strictness
        if tool_name not in agent.allowed_tools:
            return PolicyDecision(
                allowed=False,
                correction=CorrectionInfo(
                    violation=ViolationType.DISALLOWED_TOOL,
                    message=(
                        f"Agent '{agent_id}' is not permitted to call '{tool_name}'. "
                        f"Allowed tools: {agent.allowed_tools}."
                    ),
                    required_actions=[],
                    call_history=self.call_log.history_names(session_id, agent_id),
                ),
            )

        # 3. sequence / prerequisite checks
        missing: list[str] = []
        for rule in agent.sequences:
            if rule.before != tool_name:
                continue
            for req in rule.requires:
                if not self.call_log.has_satisfied(
                    session_id, agent_id, req, rule.match_fields, params
                ):
                    missing.append(req)

        if missing:
            correction = CorrectionInfo(
                violation=ViolationType.MISSING_PREREQUISITE,
                message=(
                    f"{', '.join(missing)} required before '{tool_name}'. "
                    f"Please call {', '.join(missing)} first"
                    + (f" with matching {self._match_field_names(agent, tool_name)}." if missing else ".")
                ),
                required_actions=missing,
                call_history=self.call_log.history_names(session_id, agent_id),
            )
            enforce = agent.strictness == "strict"
            return PolicyDecision(allowed=not enforce, correction=correction, enforce=enforce)

        return PolicyDecision(allowed=True)

    def _match_field_names(self, agent: AgentPolicy, tool_name: str) -> str:
        fields: set[str] = set()
        for rule in agent.sequences:
            if rule.before == tool_name:
                fields.update(rule.match_fields)
        return ", ".join(sorted(fields)) if fields else "parameters"

    def resolve_trust(self, agent_id: str, tool_name: str, default_trust: str) -> str:
        agent = self.agent_policy(agent_id)
        if agent and tool_name in agent.trust_overrides:
            return agent.trust_overrides[tool_name]
        return default_trust

    def strictness_for(self, agent_id: str, default_strictness: str) -> str:
        agent = self.agent_policy(agent_id)
        return agent.strictness if agent else default_strictness
