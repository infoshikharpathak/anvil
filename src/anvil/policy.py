"""PolicyEngine — evaluates the active policy for a given agent/tool/call."""

from __future__ import annotations

from dataclasses import dataclass

from .call_log import CallLog
from .models import AgentPolicy, CorrectionInfo, Grant, PolicyConfig, SequenceRule, ViolationType
from .namespaces import app_of, relative, resolve


class GrantTable:
    def __init__(self, grants: list[Grant]) -> None:
        self._tools = {(g.tool, a) for g in grants if g.tool for a in g.to_apps}
        self._agents = {(g.agent, a) for g in grants if g.agent for a in g.to_apps}

    def tool(self, tool_id: str, app: str) -> bool:
        return (tool_id, app) in self._tools

    def agent(self, agent_id: str, app: str) -> bool:
        return (agent_id, app) in self._agents


def resolve_agent_policy(agent_id: str, agent: AgentPolicy) -> AgentPolicy:
    """Copy of `agent` with every tool/agent reference resolved to a
    canonical ID relative to the agent's own app. Config is never rewritten
    in place, so authored policies round-trip unchanged."""
    app = app_of(agent_id)
    return agent.model_copy(
        update={
            "allowed_tools": [resolve(t, app) for t in agent.allowed_tools],
            "allowed_delegations": [resolve(a, app) for a in agent.allowed_delegations],
            "trust_overrides": {resolve(t, app): v for t, v in agent.trust_overrides.items()},
            "sequences": [
                SequenceRule(
                    before=resolve(r.before, app),
                    requires=[resolve(q, app) for q in r.requires],
                    match_fields=list(r.match_fields),
                )
                for r in agent.sequences
            ],
        }
    )


@dataclass
class PolicyDecision:
    allowed: bool
    correction: CorrectionInfo | None = None
    # When allowed is False but strictness is "loose" for a sequence/schema
    # violation, `enforce` is False: the caller should log and proceed anyway.
    enforce: bool = True


class PolicyEngine:
    def __init__(
        self, policy: PolicyConfig, call_log: CallLog, grants: list[Grant] | None = None
    ) -> None:
        self.policy = policy
        self.call_log = call_log
        self.grants = GrantTable(grants or [])

    def agent_policy(self, agent_id: str) -> AgentPolicy | None:
        """Resolved policy for a canonical agent ID. Resolved per lookup
        (cheap) so changes made to the policy after construction apply."""
        agent = self.policy.agents.get(agent_id)
        return resolve_agent_policy(agent_id, agent) if agent is not None else None

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
            return _unknown_agent(agent_id)

        agent_app, tool_app = app_of(agent_id), app_of(tool_name)

        # Corrections name tools the way this agent calls them: bare for its
        # own app, "app__tool" for others, matching the exported schemas.
        def rel(t: str) -> str:
            return relative(t, agent_app)

        # 2. cross-app tool without a grant -> hard block, ignores strictness
        if tool_app != agent_app and not self.tool_granted(tool_name, agent_app):
            return _cross_app_denied(
                f"Agent '{agent_id}' (app '{agent_app}') may not call '{rel(tool_name)}' "
                f"(app '{tool_app}'): no grant gives app '{agent_app}' access to it.",
                self.call_log.history_names(session_id, agent_id),
            )

        # 3. disallowed tool -> hard block, always enforced regardless of strictness
        if tool_name not in agent.allowed_tools:
            return PolicyDecision(
                allowed=False,
                correction=CorrectionInfo(
                    violation=ViolationType.DISALLOWED_TOOL,
                    message=(
                        f"Agent '{agent_id}' is not permitted to call '{rel(tool_name)}'. "
                        f"Allowed tools: {[rel(t) for t in agent.allowed_tools]}."
                    ),
                    required_actions=[],
                    call_history=self.call_log.history_names(session_id, agent_id),
                ),
            )

        # 4. sequence / prerequisite checks
        missing: list[str] = []
        for rule in agent.sequences:
            if rule.before != tool_name:
                continue
            for req in rule.requires:
                if not self.call_log.has_satisfied(
                    session_id, agent_id, req, rule.match_fields, params
                ):
                    missing.append(rel(req))

        if missing:
            correction = CorrectionInfo(
                violation=ViolationType.MISSING_PREREQUISITE,
                message=(
                    f"{', '.join(missing)} required before '{rel(tool_name)}'. "
                    f"Please call {', '.join(missing)} first"
                    + (f" with matching {self._match_field_names(agent, tool_name)}." if missing else ".")
                ),
                required_actions=missing,
                call_history=self.call_log.history_names(session_id, agent_id),
            )
            enforce = agent.strictness == "strict"
            return PolicyDecision(allowed=not enforce, correction=correction, enforce=enforce)

        return PolicyDecision(allowed=True)

    def check_agent_known(self, agent_id: str) -> PolicyDecision:
        if self.agent_policy(agent_id) is None:
            return _unknown_agent(agent_id)
        return PolicyDecision(allowed=True)

    def check_handoff(self, session_id: str, from_agent: str, to_agent: str) -> PolicyDecision:
        """Delegation check, mirroring allowed_tools: both agents must exist
        and `to_agent` must be in `from_agent`'s allowed_delegations. Always a
        hard block — strictness does not apply."""
        source = self.agent_policy(from_agent)
        if source is None:
            return _unknown_agent(from_agent)
        if self.agent_policy(to_agent) is None:
            return _unknown_agent(to_agent)
        from_app, to_app = app_of(from_agent), app_of(to_agent)
        if from_app != to_app and not self.agent_granted(to_agent, from_app):
            return _cross_app_denied(
                f"Agent '{from_agent}' (app '{from_app}') may not hand off to '{to_agent}' "
                f"(app '{to_app}'): no grant gives app '{from_app}' access to it.",
                self.call_log.history_names(session_id, from_agent),
            )
        if to_agent not in source.allowed_delegations:
            return PolicyDecision(
                allowed=False,
                correction=CorrectionInfo(
                    violation=ViolationType.DISALLOWED_DELEGATION,
                    message=(
                        f"Agent '{from_agent}' is not permitted to hand off to '{to_agent}'. "
                        f"Allowed delegations: {source.allowed_delegations}."
                    ),
                    required_actions=[],
                    call_history=self.call_log.history_names(session_id, from_agent),
                ),
            )
        return PolicyDecision(allowed=True)

    def tool_granted(self, tool_id: str, app: str) -> bool:
        """Whether a grant lets `app` use another app's tool."""
        return self.grants.tool(tool_id, app)

    def agent_granted(self, agent_id: str, app: str) -> bool:
        """Whether a grant lets `app` delegate to another app's agent."""
        return self.grants.agent(agent_id, app)

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


def _unknown_agent(agent_id: str) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        correction=CorrectionInfo(
            violation=ViolationType.UNKNOWN_AGENT,
            message=f"Agent '{agent_id}' is not defined in the active policy.",
            required_actions=[],
            call_history=[],
        ),
    )


def _cross_app_denied(message: str, history: list[str]) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        correction=CorrectionInfo(
            violation=ViolationType.CROSS_APP_DENIED,
            message=message,
            required_actions=[],
            call_history=history,
        ),
    )
