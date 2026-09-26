"""YAML config loading and fail-fast validation for anvil.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import AnvilConfig


class ConfigError(Exception):
    """Raised when anvil.yaml is structurally invalid. Anvil refuses to start."""


def load_config(path: str | Path) -> AnvilConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return load_config_dict(raw)


def load_config_dict(raw: dict[str, Any]) -> AnvilConfig:
    config = AnvilConfig.model_validate(raw)
    validate_config(config)
    return config


def validate_config(config: AnvilConfig) -> None:
    """Raise ConfigError on any structural inconsistency. Called at load time."""
    errors: list[str] = []
    tool_names = set(config.tools.keys())

    for policy_name, policy in config.policies.items():
        for agent_id, agent in policy.agents.items():
            ctx = f"policy '{policy_name}' agent '{agent_id}'"
            allowed = set(agent.allowed_tools)

            # allowed_tools must reference registered tools
            for t in agent.allowed_tools:
                if t not in tool_names:
                    errors.append(f"{ctx}: allowed_tools references unregistered tool '{t}'")

            # trust_overrides must reference tools in allowed_tools
            for t in agent.trust_overrides:
                if t not in allowed:
                    errors.append(f"{ctx}: trust_overrides references '{t}' not in allowed_tools")

            # sequence rules must reference tools in allowed_tools and be registered
            for rule in agent.sequences:
                if rule.before not in tool_names:
                    errors.append(f"{ctx}: sequence 'before' references unregistered tool '{rule.before}'")
                elif rule.before not in allowed:
                    errors.append(f"{ctx}: sequence 'before={rule.before}' not in allowed_tools")

                for req in rule.requires:
                    if req not in tool_names:
                        errors.append(f"{ctx}: sequence 'requires' references unregistered tool '{req}'")
                    elif req not in allowed:
                        errors.append(f"{ctx}: sequence 'requires={req}' not in allowed_tools")

                # match_fields must exist in the tool's parameter schema, if defined
                if rule.match_fields:
                    before_tool = config.tools.get(rule.before)
                    if before_tool and before_tool.parameters:
                        props = (before_tool.parameters or {}).get("properties", {})
                        for field in rule.match_fields:
                            if field not in props:
                                errors.append(
                                    f"{ctx}: match_fields '{field}' not in parameters schema "
                                    f"for tool '{rule.before}'"
                                )

            # cycle detection among this agent's sequence rules
            cycle = _find_cycle(agent.sequences)
            if cycle:
                errors.append(f"{ctx}: circular prerequisite detected: {' -> '.join(cycle)}")

    if errors:
        raise ConfigError("Invalid anvil config:\n" + "\n".join(f"  - {e}" for e in errors))


def _find_cycle(sequences: list) -> list[str] | None:
    """Detect cycles in the 'before requires X' graph (X must precede 'before')."""
    # Build edges: requires -> before  (i.e. requires must happen first)
    graph: dict[str, set[str]] = {}
    for rule in sequences:
        graph.setdefault(rule.before, set()).update(rule.requires)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    path: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.get(node, ()):
            state = color.get(neighbor, WHITE)
            if state == GRAY:
                idx = path.index(neighbor)
                return path[idx:] + [neighbor]
            if state == WHITE:
                result = dfs(neighbor)
                if result:
                    return result
        path.pop()
        color[node] = BLACK
        return None

    for node in list(graph.keys()):
        if color.get(node, WHITE) == WHITE:
            result = dfs(node)
            if result:
                return result
    return None
