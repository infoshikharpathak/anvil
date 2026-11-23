"""LLM-safe tool names: bare for the caller's app, "app__tool" for granted
cross-app tools, in schemas, execute(), and corrections alike."""

import pytest
from fastapi.testclient import TestClient

from anvil.client import Anvil
from anvil.config import ConfigError, load_config_dict
from anvil.models import ViolationType
from anvil.registry.app import app as registry_app
from anvil.registry.auth import hash_api_key
from anvil.registry.storage import RegistryStorage

APP_KEY = "ik_test_ironclad_0123456789"


def _raw(with_keys=False):
    return {
        "apps": {
            "ironclad": {
                "base_url": "http://bank",
                "api_keys": {"ironclad-agent": hash_api_key(APP_KEY)} if with_keys else {},
                "tools": {
                    "send_email": {"path": "/bank-email", "trust": "trusted", "description": "ironclad's own"},
                    "get_accounts": {"path": "/a", "trust": "trusted"},
                },
            },
            "notify": {
                "base_url": "http://notify",
                "tools": {
                    "send_email": {"path": "/email", "trust": "trusted", "description": "notify's"},
                    "send_sms": {"path": "/sms", "trust": "trusted"},
                },
            },
        },
        "tools": {"legacy": {"endpoint": "http://legacy", "trust": "trusted"}},
        "policies": {
            "p": {
                "agents": {
                    "ironclad.banking_agent": {
                        "allowed_tools": ["send_email", "get_accounts", "notify.send_email"],
                        "sequences": [{"before": "send_email", "requires": ["notify__send_email"]}],
                    },
                    "notify.dispatcher": {"allowed_tools": ["send_email", "send_sms"]},
                }
            }
        },
        "grants": [{"tool": "notify.send_email", "to_apps": ["ironclad"]}],
    }


def registry(with_keys=False):
    registry_app.state.storage = RegistryStorage(load_config_dict(_raw(with_keys)))
    return TestClient(registry_app)


def schema_name(client, tool, fmt="openai", **params):
    resp = client.get(f"/tools/{tool}/schema", params={"format": fmt, **params}, headers=params.pop("headers", {}))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["function"]["name"] if fmt == "openai" else body["name"]


def test_granted_cross_app_tool_exported_as_app__tool():
    c = registry()
    agent = {"agent": "ironclad.banking_agent"}
    assert schema_name(c, "notify.send_email", **agent) == "notify__send_email"
    assert schema_name(c, "notify.send_email", "anthropic", **agent) == "notify__send_email"
    # same-app tools stay bare, even though notify has a tool of the same short name
    assert schema_name(c, "ironclad.send_email", **agent) == "send_email"
    # the path accepts any spelling execute() accepts, resolved in the agent's app
    raw = c.get("/tools/send_email/schema", params=agent).json()
    assert raw["name"] == "ironclad.send_email" and raw["description"] == "ironclad's own"
    raw = c.get("/tools/notify__send_email/schema", params=agent).json()
    assert raw["name"] == "notify.send_email" and raw["description"] == "notify's"


def test_names_relative_to_each_calling_app():
    c = registry()
    assert schema_name(c, "send_email", agent="notify.dispatcher") == "send_email"
    assert schema_name(c, "ironclad.send_email", agent="notify.dispatcher") == "ironclad__send_email"
    assert schema_name(c, "default.legacy", agent="ironclad.banking_agent") == "default__legacy"
    # a bare name always means the caller's own app
    assert c.get("/tools/legacy/schema", params={"agent": "ironclad.banking_agent"}).status_code == 404
    # no calling app known: named relative to the tool's own app (bare), as before
    assert schema_name(c, "notify.send_email") == "send_email"


def test_schema_names_follow_caller_app_from_api_key():
    c = registry(with_keys=True)
    h = {"X-Client-Id": "ironclad-agent", "Authorization": f"Bearer {APP_KEY}"}
    resp = c.get("/tools/notify.send_email/schema", params={"format": "openai"}, headers=h)
    assert resp.json()["function"]["name"] == "notify__send_email"
    resp = c.get("/tools/send_email/schema", params={"format": "openai"}, headers=h)
    assert resp.json()["function"]["description"] == "ironclad's own"
    # ungranted notify tool stays invisible; another app's agent is refused
    assert c.get("/tools/notify__send_sms/schema", headers=h).status_code == 404
    assert c.get("/tools/send_email/schema", params={"agent": "notify.dispatcher"}, headers=h).status_code == 403


def make_anvil():
    client = Anvil(load_config_dict(_raw()), "p", app="ironclad", session_id="s")
    seen = []

    async def fake(tool_cfg, params):
        seen.append((tool_cfg.name, tool_cfg.endpoint))
        return {"ok": True}

    client._call_upstream = fake
    return client, seen


@pytest.mark.asyncio
async def test_each_exported_name_routes_to_the_right_tool():
    client, seen = make_anvil()
    assert (await client.execute("banking_agent", "notify__send_email", {})).success
    assert (await client.execute("banking_agent", "send_email", {})).success
    assert seen == [
        ("notify.send_email", "http://notify/email"),
        ("ironclad.send_email", "http://bank/bank-email"),
    ]


@pytest.mark.asyncio
async def test_corrections_use_the_same_spelling():
    client, _ = make_anvil()
    blocked = await client.execute("banking_agent", "send_email", {})
    assert blocked.correction.violation == ViolationType.MISSING_PREREQUISITE
    assert blocked.correction.required_actions == ["notify__send_email"]
    assert "Please call notify__send_email first" in blocked.correction.message
    # feeding required_actions straight back works
    assert (await client.execute("banking_agent", blocked.correction.required_actions[0], {})).success
    assert (await client.execute("banking_agent", "send_email", {})).success

    denied = await client.execute("banking_agent", "notify__send_sms", {})
    assert denied.correction.violation == ViolationType.CROSS_APP_DENIED
    assert "'notify__send_sms'" in denied.correction.message

    disallowed = await client.execute("banking_agent", "default__legacy", {})
    assert disallowed.correction.violation == ViolationType.CROSS_APP_DENIED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["apps"].update({"bad__app": {}}),
        lambda r: r["apps"]["notify"]["tools"].update({"send__fax": {"path": "/f"}}),
        lambda r: r["tools"].update({"my__tool": {"endpoint": "http://t"}}),
        lambda r: r["policies"]["p"]["agents"].update({"ironclad.two__words": {}}),
    ],
)
def test_double_underscore_names_rejected(mutate):
    raw = _raw()
    mutate(raw)
    with pytest.raises(ConfigError, match="must not contain"):
        load_config_dict(raw)
