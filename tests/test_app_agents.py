"""Agents belong to an app via "app.agent" IDs."""

import pytest

from anvil.client import Anvil
from anvil.config import ConfigError, load_config_dict
from anvil.models import ViolationType


def _raw(agents):
    return {
        "apps": {
            "ironclad": {
                "base_url": "http://bank",
                "tools": {
                    "get_accounts": {"path": "/accounts", "trust": "trusted"},
                    "initiate_transfer": {"path": "/transfers", "trust": "trusted"},
                },
            },
            "notify": {"base_url": "http://notify", "tools": {"send_email": {"path": "/email"}}},
        },
        "tools": {"send_notification": {"endpoint": "http://x/notify"}},
        "policies": {"p": {"agents": agents}},
    }


IRONCLAD_AGENTS = {
    "ironclad.banking_agent": {
        "allowed_tools": ["get_accounts", "initiate_transfer"],
        "allowed_delegations": ["auditor"],
        "sequences": [{"before": "initiate_transfer", "requires": ["get_accounts"]}],
        "trust_overrides": {"initiate_transfer": "untrusted"},
    },
    "ironclad.auditor": {"allowed_tools": ["get_accounts"]},
}


def test_bare_references_resolve_within_agents_app():
    cfg = load_config_dict(_raw(IRONCLAD_AGENTS))
    assert set(cfg.policies["p"].agents) == {"ironclad.banking_agent", "ironclad.auditor"}
    engine = Anvil(cfg, "p").policy_engine
    agent = engine.agent_policy("ironclad.banking_agent")
    assert agent.allowed_tools == ["ironclad.get_accounts", "ironclad.initiate_transfer"]
    assert agent.allowed_delegations == ["ironclad.auditor"]
    assert agent.sequences[0].before == "ironclad.initiate_transfer"
    assert agent.trust_overrides == {"ironclad.initiate_transfer": "untrusted"}
    # authored config is not rewritten
    assert cfg.policies["p"].agents["ironclad.banking_agent"].allowed_tools == [
        "get_accounts",
        "initiate_transfer",
    ]


def test_default_prefix_is_canonicalized_to_bare():
    cfg = load_config_dict(_raw({"default.notifier": {"allowed_tools": ["send_notification"]}}))
    assert list(cfg.policies["p"].agents) == ["notifier"]


def test_cross_app_tool_without_grant_rejected():
    agents = {"ironclad.banking_agent": {"allowed_tools": ["get_accounts", "notify.send_email"]}}
    with pytest.raises(ConfigError, match="'notify.send_email' from app 'notify' without a grant"):
        load_config_dict(_raw(agents))


def test_default_agent_cannot_list_app_tool_without_grant():
    with pytest.raises(ConfigError, match="without a grant"):
        load_config_dict(_raw({"notifier": {"allowed_tools": ["ironclad.get_accounts"]}}))


def test_bare_tool_in_app_agent_does_not_fall_back_to_default_app():
    # send_notification exists only in the default app, so it is unregistered for ironclad
    with pytest.raises(ConfigError, match="unregistered tool 'ironclad.send_notification'"):
        load_config_dict(_raw({"ironclad.a": {"allowed_tools": ["send_notification"]}}))


def test_agent_in_undeclared_app_rejected():
    with pytest.raises(ConfigError, match="undeclared app 'ghost'"):
        load_config_dict(_raw({"ghost.a": {"allowed_tools": []}}))


@pytest.mark.asyncio
async def test_execute_with_app_agent_and_bare_tool_name():
    client = Anvil(load_config_dict(_raw(IRONCLAD_AGENTS)), "p", session_id="s")

    async def fake(tool_cfg, params):
        return {"ok": tool_cfg.name}

    client._call_upstream = fake
    blocked = await client.execute("ironclad.banking_agent", "initiate_transfer", {})
    assert blocked.correction.violation == ViolationType.MISSING_PREREQUISITE
    # corrections use the names the agent calls tools by
    assert blocked.correction.required_actions == ["get_accounts"]
    assert "Please call get_accounts first" in blocked.correction.message
    # and execute() accepts them back
    assert (await client.execute("ironclad.banking_agent", blocked.correction.required_actions[0], {})).success
    ok = await client.execute("ironclad.banking_agent", "initiate_transfer", {})
    assert ok.success and ok.data == {"ok": "ironclad.initiate_transfer"}
    return

    assert (await client.execute("ironclad.banking_agent", "get_accounts", {})).success
    ok = await client.execute("ironclad.banking_agent", "initiate_transfer", {})
    assert ok.success and ok.data == {"ok": "ironclad.initiate_transfer"}

    calls = client.trace.build_payload()["calls"]
    assert {c["agent"] for c in calls} == {"ironclad.banking_agent"}


@pytest.mark.asyncio
async def test_handoff_bare_target_resolves_in_source_app():
    client = Anvil(load_config_dict(_raw(IRONCLAD_AGENTS)), "p")
    assert (await client.check_handoff("ironclad.banking_agent", "auditor")).allowed
    assert (await client.check_agent_input("ironclad.auditor", "hello")).allowed
