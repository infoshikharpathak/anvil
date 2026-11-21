"""Call-time cross-app enforcement (CROSS_APP_DENIED)."""

import pytest

from anvil.client import Anvil
from anvil.config import ConfigError, load_config_dict
from anvil.models import AnvilConfig, ViolationType

RAW = {
    "apps": {
        "ironclad": {"base_url": "http://bank", "tools": {"get_accounts": {"path": "/a", "trust": "trusted"}}},
        "notify": {"base_url": "http://notify", "tools": {"send_email": {"path": "/e", "trust": "trusted"}}},
    },
    "policies": {
        "p": {
            "agents": {
                # Unvalidated on purpose: a registry bundle or hand-built config
                # can reach execute() without passing validate_config.
                "ironclad.banker": {
                    "allowed_tools": ["get_accounts", "notify.send_email"],
                    "allowed_delegations": ["notify.dispatcher"],
                    "strictness": "loose",
                },
                "notify.dispatcher": {"allowed_tools": ["send_email"]},
            }
        }
    },
}


def make():
    client = Anvil(AnvilConfig.model_validate(RAW), "p", session_id="s")

    async def fake(tool_cfg, params):
        raise AssertionError("upstream must not be called")

    client._call_upstream = fake
    return client


@pytest.mark.asyncio
async def test_cross_app_tool_call_is_hard_block_even_when_loose():
    client = make()
    resp = await client.execute("ironclad.banker", "notify.send_email", {"to": "x"})
    assert resp.success is False and resp.tool_executed is False
    assert resp.correction.violation == ViolationType.CROSS_APP_DENIED
    assert "app 'ironclad'" in resp.correction.message
    call = client.trace.build_payload()["calls"][0]
    assert call["result"] == "blocked" and call["violation"] == "cross_app_denied"


@pytest.mark.asyncio
async def test_cross_app_checked_before_allowed_tools():
    # dispatcher never lists ironclad.get_accounts; the cross-app reason wins
    resp = await make().execute("notify.dispatcher", "ironclad.get_accounts", {})
    assert resp.correction.violation == ViolationType.CROSS_APP_DENIED


@pytest.mark.asyncio
async def test_cross_app_handoff_denied():
    res = await make().check_handoff("ironclad.banker", "notify.dispatcher")
    assert not res.allowed
    assert res.correction.violation == ViolationType.CROSS_APP_DENIED


@pytest.mark.asyncio
async def test_same_app_calls_unaffected():
    client = make()

    async def ok(tool_cfg, params):
        return {"accounts": []}

    client._call_upstream = ok
    assert (await client.execute("ironclad.banker", "get_accounts", {})).success


def test_config_rejects_cross_app_delegation_without_grant():
    raw = {
        "apps": RAW["apps"],
        "policies": {
            "p": {
                "agents": {
                    "ironclad.banker": {"allowed_delegations": ["notify.dispatcher"]},
                    "notify.dispatcher": {},
                }
            }
        },
    }
    with pytest.raises(ConfigError, match="'notify.dispatcher' from app 'notify' without a grant"):
        load_config_dict(raw)
