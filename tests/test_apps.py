"""Apps in config: app-scoped tools, "app.tool" IDs, backward compat."""

import pytest
from fastapi.testclient import TestClient

from anvil.client import Anvil
from anvil.config import ConfigError, load_config_dict
from anvil.models import AnvilConfig
from anvil.namespaces import canonical, resolve, split_id


def _apps_config(**extra):
    raw = {
        "apps": {
            "ironclad": {
                "base_url": "http://bank:8001/",
                "auth": {"header": "Authorization", "secret_env": "IRONCLAD_TOKEN", "prefix": "Bearer "},
                "tools": {
                    "get_accounts": {"path": "/accounts", "method": "GET", "trust": "trusted"},
                    "external": {"endpoint": "http://elsewhere/x"},
                },
            }
        },
        "tools": {"send_notification": {"endpoint": "http://x/notify"}},
        "policies": {},
    }
    raw.update(extra)
    return raw


def test_namespace_helpers():
    assert split_id("ironclad.get_accounts") == ("ironclad", "get_accounts")
    assert split_id("send_notification") == ("default", "send_notification")
    assert canonical("default.send_notification") == "send_notification"
    assert resolve("get_accounts", "ironclad") == "ironclad.get_accounts"
    assert resolve("notify.send_email", "ironclad") == "notify.send_email"
    assert resolve("default.x", "ironclad") == "x"


def test_app_tools_get_qualified_ids_and_resolved_endpoints():
    cfg = load_config_dict(_apps_config())
    assert set(cfg.tools) == {"ironclad.get_accounts", "ironclad.external", "send_notification"}
    acct = cfg.tools["ironclad.get_accounts"]
    assert acct.name == "ironclad.get_accounts"
    assert acct.app == "ironclad"
    assert acct.endpoint == "http://bank:8001/accounts"
    assert acct.path is None
    assert cfg.tools["ironclad.external"].endpoint == "http://elsewhere/x"
    assert cfg.apps["ironclad"].tools == {}
    assert cfg.apps["ironclad"].auth.secret_env == "IRONCLAD_TOKEN"


def test_top_level_tools_are_default_app_with_bare_ids():
    cfg = load_config_dict(_apps_config())
    note = cfg.tools["send_notification"]
    assert note.app == "default" and note.name == "send_notification"


def test_qualified_top_level_key_joins_its_app():
    raw = _apps_config()
    raw["tools"]["ironclad.reset"] = {"path": "reset"}
    cfg = load_config_dict(raw)
    assert cfg.tools["ironclad.reset"].endpoint == "http://bank:8001/reset"
    assert cfg.tools["ironclad.reset"].app == "ironclad"


def test_flattened_config_round_trips_unchanged():
    cfg = load_config_dict(_apps_config())
    again = AnvilConfig.model_validate(cfg.model_dump())
    assert again.model_dump() == cfg.model_dump()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["apps"]["ironclad"]["tools"]["get_accounts"].update(endpoint="http://a"), "both endpoint and path"),
        (lambda r: r["apps"]["ironclad"].pop("base_url"), "has no base_url"),
        (lambda r: r["tools"].update({"bare": {}}), "needs an endpoint or a path"),
        (lambda r: r["tools"].update({"ghost.t": {"endpoint": "http://g"}}), "undeclared app 'ghost'"),
        (lambda r: r["tools"].update({"ironclad.get_accounts": {"endpoint": "http://dup"}}), "declared more than once"),
        (lambda r: r["apps"].update({"a.b": {}}), "must not contain '.'"),
    ],
)
def test_bad_app_configs_rejected(mutate, message):
    raw = _apps_config()
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        load_config_dict(raw)


@pytest.mark.asyncio
async def test_execute_accepts_qualified_tool_id():
    raw = _apps_config(
        policies={"p": {"agents": {"a": {"allowed_tools": ["ironclad.get_accounts"]}}}}
    )
    client = Anvil(load_config_dict(raw), "p")
    seen = []

    async def fake(tool_cfg, params):
        seen.append(tool_cfg.endpoint)
        return {"accounts": []}

    client._call_upstream = fake
    resp = await client.execute("a", "ironclad.get_accounts", {})
    assert resp.success and seen == ["http://bank:8001/accounts"]
    assert client.trace.build_payload()["calls"][0]["tool"] == "ironclad.get_accounts"


def test_register_tool_with_app_prefix():
    client = Anvil(load_config_dict(_apps_config(policies={"p": {"agents": {}}})), "p")
    client.register_tool("ironclad.extra", endpoint="http://e")
    cfg = client._resolve_tool("ironclad.extra")
    assert cfg.app == "ironclad" and cfg.name == "ironclad.extra"


def test_registry_schema_export_uses_short_name():
    from registry.app import app
    from registry.storage import RegistryStorage

    app.state.storage = RegistryStorage(load_config_dict(_apps_config()))
    c = TestClient(app)
    assert "ironclad.get_accounts" in c.get("/tools").json()
    body = c.get("/tools/ironclad.get_accounts/schema", params={"format": "openai"}).json()
    assert body["function"]["name"] == "get_accounts"
    raw = c.get("/tools/ironclad.get_accounts/schema").json()
    assert raw["name"] == "ironclad.get_accounts" and raw["app"] == "ironclad"
