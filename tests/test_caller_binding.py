"""Caller binding: Anvil(app=...) in library mode, per-app API keys in the registry."""

import functools

import httpx
import pytest
from fastapi.testclient import TestClient

from anvil.client import Anvil, AnvilError
from anvil.config import ConfigError, load_config_dict
from anvil.models import ViolationType
from registry.app import app as registry_app
from registry.auth import hash_api_key
from registry.storage import RegistryStorage

IRONCLAD_KEY = "ik_test_ironclad_0123456789"
NOTIFY_KEY = "ik_test_notify_9876543210"


def _raw(with_keys=True):
    return {
        "apps": {
            "ironclad": {
                "base_url": "http://bank",
                "api_keys": {"ironclad-agent": hash_api_key(IRONCLAD_KEY)} if with_keys else {},
                "tools": {"get_accounts": {"path": "/a", "trust": "trusted"}},
            },
            "notify": {
                "base_url": "http://notify",
                "api_keys": {"notify-svc": hash_api_key(NOTIFY_KEY)} if with_keys else {},
                "tools": {
                    "send_email": {"path": "/e", "trust": "trusted"},
                    "internal": {"path": "/i", "trust": "trusted"},
                },
            },
        },
        "tools": {"legacy": {"endpoint": "http://legacy", "trust": "trusted"}},
        "policies": {
            "ironclad-banking": {
                "agents": {
                    "ironclad.banking_agent": {
                        "allowed_tools": ["get_accounts", "notify.send_email"],
                        "allowed_delegations": ["auditor"],
                    },
                    "ironclad.auditor": {},
                }
            },
            "notify-ops": {"agents": {"notify.dispatcher": {"allowed_tools": ["send_email", "internal"]}}},
            "legacy": {"agents": {"old_agent": {"allowed_tools": ["legacy"]}}},
        },
        "grants": [{"tool": "notify.send_email", "to_apps": ["ironclad"]}],
    }


# ---------------------------------------------------------------- library


def bound(app="ironclad"):
    client = Anvil(load_config_dict(_raw()), "ironclad-banking", app=app, session_id="s")

    async def fake(tool_cfg, params):
        return {"tool": tool_cfg.name}

    client._call_upstream = fake
    return client


@pytest.mark.asyncio
async def test_bound_instance_resolves_bare_agent_ids_to_its_app():
    client = bound()
    resp = await client.execute("banking_agent", "get_accounts", {})
    assert resp.success and resp.data == {"tool": "ironclad.get_accounts"}
    assert (await client.execute("ironclad.banking_agent", "notify.send_email", {})).success
    assert (await client.check_handoff("banking_agent", "auditor")).allowed
    assert (await client.check_agent_output("auditor", "fine")).allowed


@pytest.mark.asyncio
async def test_bound_instance_rejects_other_apps_agents_everywhere():
    client = bound()
    results = [
        (await client.execute("notify.dispatcher", "send_email", {})).correction,
        (await client.check_agent_input("notify.dispatcher", "hi")).correction,
        (await client.check_agent_output("default.old_agent", "hi")).correction,
        (await client.check_handoff("notify.dispatcher", "ironclad.auditor")).correction,
    ]
    for correction in results:
        assert correction.violation == ViolationType.CROSS_APP_DENIED
        assert "bound to app 'ironclad'" in correction.message
    calls = client.trace.build_payload()["calls"]
    assert len(calls) == 4 and all(c["result"] == "blocked" for c in calls)


def test_binding_to_undeclared_app_fails_fast():
    with pytest.raises(AnvilError, match="App 'ghost' is not declared"):
        Anvil(load_config_dict(_raw()), "ironclad-banking", app="ghost")


@pytest.mark.asyncio
async def test_unbound_instance_unchanged():
    client = Anvil(load_config_dict(_raw()), "legacy")

    async def fake(tool_cfg, params):
        return {}

    client._call_upstream = fake
    assert (await client.execute("old_agent", "legacy", {})).success


def test_config_rejects_plaintext_key_and_shared_client_id():
    raw = _raw()
    raw["apps"]["ironclad"]["api_keys"] = {"ironclad-agent": IRONCLAD_KEY}
    with pytest.raises(ConfigError, match="must be a SHA-256 hex digest"):
        load_config_dict(raw)
    raw = _raw()
    raw["apps"]["notify"]["api_keys"]["ironclad-agent"] = hash_api_key("x")
    with pytest.raises(ConfigError, match="client_id 'ironclad-agent' has API keys in both"):
        load_config_dict(raw)


# ---------------------------------------------------------------- registry


def auth(client_id, key):
    return {"X-Client-Id": client_id, "Authorization": f"Bearer {key}"}


IRON = auth("ironclad-agent", IRONCLAD_KEY)
NOTIFY = auth("notify-svc", NOTIFY_KEY)


@pytest.fixture
def registry():
    registry_app.state.storage = RegistryStorage(load_config_dict(_raw()))
    return TestClient(registry_app)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Client-Id": "ironclad-agent"},
        auth("ironclad-agent", "wrong"),
        auth("ironclad-agent", NOTIFY_KEY),  # right key, wrong client_id
        auth("nobody", IRONCLAD_KEY),
        {"X-Client-Id": "ironclad-agent", "Authorization": IRONCLAD_KEY},  # no Bearer
    ],
)
def test_registry_rejects_missing_or_bad_credentials(registry, headers):
    for path in ("/policies", "/policies/ironclad-banking", "/tools", "/traces", "/checkouts"):
        assert registry.get(path, headers=headers).status_code == 401, path
    assert registry.post("/traces", json={"session_id": "x"}, headers=headers).status_code == 401
    assert "policies" not in registry.get("/health").json()


def test_registry_serves_only_the_callers_app(registry):
    assert set(registry.get("/policies", headers=IRON).json()) == {"ironclad-banking"}
    assert set(registry.get("/policies", headers=NOTIFY).json()) == {"notify-ops"}
    # granted tool is visible to ironclad; notify's other tools and default tools are not
    assert set(registry.get("/tools", headers=IRON).json()) == {"ironclad.get_accounts", "notify.send_email"}
    assert registry.get("/tools/notify.internal/schema", headers=IRON).status_code == 404
    assert registry.get("/tools/notify.send_email/schema", headers=IRON).status_code == 200
    # another app's policy looks like it doesn't exist
    assert registry.get("/policies/notify-ops", headers=IRON).status_code == 404
    assert registry.get("/policies/legacy", headers=IRON).status_code == 404


def test_registry_bundle_is_scoped_and_has_no_key_digests(registry):
    bundle = registry.get("/policies/ironclad-banking", headers={**IRON, "X-Session-Id": "s1"}).json()
    assert set(bundle["tools"]) == {"ironclad.get_accounts", "notify.send_email"}
    assert set(bundle["apps"]) == {"ironclad", "notify"}
    assert all(a["api_keys"] == {} for a in bundle["apps"].values())
    assert bundle["grants"] == [{"tool": "notify.send_email", "agent": None, "to_apps": ["ironclad"]}]
    assert hash_api_key(IRONCLAD_KEY) not in str(bundle)
    checkout = registry.get("/checkouts", headers=IRON).json()[0]
    assert checkout["client_id"] == "ironclad-agent" and checkout["app"] == "ironclad"
    assert registry.get("/checkouts", headers=NOTIFY).json() == []


def test_registry_policy_writes_are_scoped(registry):
    mine = {"agents": {"ironclad.helper": {"allowed_tools": ["get_accounts"]}}}
    theirs = {"agents": {"notify.helper": {"allowed_tools": ["send_email"]}}}
    assert registry.post("/policies", params={"name": "n1"}, json=mine, headers=IRON).status_code == 200
    assert registry.post("/policies", params={"name": "n2"}, json=theirs, headers=IRON).status_code == 403
    assert registry.put("/policies/notify-ops", json=mine, headers=IRON).status_code == 404
    assert registry.put("/policies/ironclad-banking", json=theirs, headers=IRON).status_code == 403
    assert registry.delete("/policies/notify-ops", headers=IRON).status_code == 404
    assert registry.delete("/policies/n1", headers=IRON).status_code == 200


def test_registry_traces_are_scoped(registry):
    trace = {"session_id": "t1", "policy": "ironclad-banking", "started_at": "a", "ended_at": "b"}
    assert registry.post("/traces", json=trace, headers=IRON).status_code == 200
    assert registry.post("/traces", json={**trace, "session_id": "t2"}, headers=NOTIFY).status_code == 403
    assert [t["session_id"] for t in registry.get("/traces", headers=IRON).json()] == ["t1"]
    assert registry.get("/traces", headers=NOTIFY).json() == []
    assert registry.get("/traces/t1", headers=NOTIFY).status_code == 404
    stored = registry.get("/traces/t1", headers=IRON).json()
    assert stored["app"] == "ironclad" and stored["client_id"] == "ironclad-agent"


def test_registry_open_mode_unchanged_without_keys():
    registry_app.state.storage = RegistryStorage(load_config_dict(_raw(with_keys=False)))
    c = TestClient(registry_app)
    assert set(c.get("/policies").json()) == {"ironclad-banking", "notify-ops", "legacy"}
    assert c.get("/policies/notify-ops").status_code == 200
    assert "policies" in c.get("/health").json()


@pytest.mark.asyncio
async def test_library_end_to_end_with_registry_key(registry, monkeypatch):
    transport = httpx.ASGITransport(app=registry_app)
    monkeypatch.setattr(httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport))

    client = await Anvil.from_registry(
        "http://registry",
        policy="ironclad-banking",
        client_id="ironclad-agent",
        client_key=IRONCLAD_KEY,
        app="ironclad",
    )

    async def fake(tool_cfg, params):
        return {"accounts": []}

    client._call_upstream = fake
    assert (await client.execute("banking_agent", "get_accounts", {})).success
    assert await client.end_session() is True

    stored = registry.get(f"/traces/{client.session_id}", headers=IRON).json()
    assert stored["app"] == "ironclad"
    assert stored["calls"][0]["tool"] == "ironclad.get_accounts"

    with pytest.raises(Exception, match="401"):
        await Anvil.from_registry("http://registry", policy="ironclad-banking", client_id="ironclad-agent")
