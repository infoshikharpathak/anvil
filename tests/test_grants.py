"""Grants: let other apps use a tool or delegate to an agent."""

import pytest
from fastapi.testclient import TestClient

from anvil.client import Anvil
from anvil.config import ConfigError, load_config_dict
from anvil.models import ViolationType


def _raw(grants, ironclad_tools=("get_accounts", "notify.send_email"), delegations=("notify.dispatcher",)):
    return {
        "apps": {
            "ironclad": {"base_url": "http://bank", "tools": {"get_accounts": {"path": "/a", "trust": "trusted"}}},
            "notify": {
                "base_url": "http://notify",
                "tools": {
                    "send_email": {"path": "/e", "trust": "trusted"},
                    "send_sms": {"path": "/s", "trust": "trusted"},
                },
            },
            "other": {},
        },
        "policies": {
            "p": {
                "agents": {
                    "ironclad.banker": {
                        "allowed_tools": list(ironclad_tools),
                        "allowed_delegations": list(delegations),
                    },
                    "notify.dispatcher": {"allowed_tools": ["send_email"]},
                }
            }
        },
        "grants": grants,
    }


GRANTS = [
    {"tool": "notify.send_email", "to_apps": ["ironclad"]},
    {"agent": "notify.dispatcher", "to_apps": ["ironclad"]},
]


def make(grants=GRANTS, **kw):
    client = Anvil(load_config_dict(_raw(grants, **kw)), "p", session_id="s")

    async def fake(tool_cfg, params):
        return {"sent": tool_cfg.name}

    client._call_upstream = fake
    return client


def test_granted_cross_app_tool_and_delegation_pass_validation():
    make()


@pytest.mark.parametrize(
    "grants, message",
    [
        ([{"tool": "notify.send_email", "to_apps": ["other"]}, GRANTS[1]], "send_email' from app 'notify' without a grant"),
        ([GRANTS[0]], "dispatcher' from app 'notify' without a grant"),
        ([{"tool": "notify.nope", "to_apps": ["ironclad"]}, *GRANTS], "unregistered tool 'notify.nope'"),
        ([{"agent": "notify.ghost", "to_apps": ["ironclad"]}, *GRANTS], "agent 'notify.ghost' not in any policy"),
        ([{"tool": "notify.send_email", "to_apps": ["ghost"]}, *GRANTS], "undeclared app 'ghost'"),
        ([{"tool": "notify.send_email", "to_apps": []}, *GRANTS], "to_apps is empty"),
        ([{"tool": "notify.send_email", "agent": "notify.dispatcher", "to_apps": ["ironclad"]}], "exactly one of"),
    ],
)
def test_bad_or_missing_grants_rejected(grants, message):
    with pytest.raises(ConfigError, match=message):
        load_config_dict(_raw(grants))


@pytest.mark.asyncio
async def test_granted_tool_executes():
    resp = await make().execute("ironclad.banker", "notify.send_email", {"to": "a"})
    assert resp.success and resp.data == {"sent": "notify.send_email"}


@pytest.mark.asyncio
async def test_grant_covers_only_the_named_tool():
    client = make()
    client.policy_engine.policy.agents["ironclad.banker"].allowed_tools.append("notify.send_sms")
    resp = await client.execute("ironclad.banker", "notify.send_sms", {})
    assert resp.correction.violation == ViolationType.CROSS_APP_DENIED


@pytest.mark.asyncio
async def test_grant_does_not_bypass_allowed_tools():
    client = make(ironclad_tools=("get_accounts",))
    resp = await client.execute("ironclad.banker", "notify.send_email", {})
    assert resp.correction.violation == ViolationType.DISALLOWED_TOOL


@pytest.mark.asyncio
async def test_granted_handoff_allowed_and_still_needs_allowed_delegations():
    assert (await make().check_handoff("ironclad.banker", "notify.dispatcher")).allowed
    res = await make(delegations=()).check_handoff("ironclad.banker", "notify.dispatcher")
    assert res.correction.violation == ViolationType.DISALLOWED_DELEGATION


@pytest.mark.asyncio
async def test_grant_is_one_way():
    res = await make().execute("notify.dispatcher", "ironclad.get_accounts", {})
    assert res.correction.violation == ViolationType.CROSS_APP_DENIED


def test_registry_bundle_carries_grants():
    from registry.app import app
    from registry.storage import RegistryStorage

    app.state.storage = RegistryStorage(load_config_dict(_raw(GRANTS)))
    bundle = TestClient(app).get("/policies/p").json()
    assert bundle["grants"][0]["tool"] == "notify.send_email"


def test_registry_policy_edit_validates_grants_against_all_policies():
    from registry.app import app
    from registry.storage import RegistryStorage

    app.state.storage = RegistryStorage(load_config_dict(_raw(GRANTS)))
    c = TestClient(app)
    resp = c.post("/policies", params={"name": "solo"}, json={"agents": {"ironclad.x": {"allowed_tools": ["get_accounts"]}}})
    assert resp.status_code == 200, resp.text
