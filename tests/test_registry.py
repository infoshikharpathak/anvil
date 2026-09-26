"""Tests for the registry service: policy CRUD validation, checkouts,
schema export, and UI page rendering (smoke tests via TestClient)."""

import pytest
from fastapi.testclient import TestClient

from anvil.models import AnvilConfig
from registry.app import app
from registry.storage import RegistryStorage

CONFIG_DICT = {
    "tools": {
        "verify_account": {"endpoint": "http://x/verify", "method": "POST", "trust": "trusted"},
        "get_balance": {"endpoint": "http://x/balance", "method": "GET", "trust": "trusted"},
        "send_payment": {"endpoint": "http://x/pay", "method": "POST", "trust": "untrusted"},
    },
    "policies": {
        "strict-payments": {
            "agents": {
                "payment_agent": {
                    "allowed_tools": ["verify_account", "get_balance", "send_payment"],
                    "sequences": [{"before": "send_payment", "requires": ["verify_account"]}],
                    "strictness": "strict",
                }
            }
        }
    },
}


@pytest.fixture
def client():
    app.state.storage = RegistryStorage(AnvilConfig.model_validate(CONFIG_DICT))
    return TestClient(app)


def test_create_policy_rejects_unregistered_tool(client):
    resp = client.post(
        "/policies",
        params={"name": "broken"},
        json={"agents": {"a": {"allowed_tools": ["nonexistent_tool"], "sequences": []}}},
    )
    assert resp.status_code == 422
    assert "unregistered tool" in resp.json()["detail"]


def test_create_policy_rejects_cycle(client):
    resp = client.post(
        "/policies",
        params={"name": "cyclic"},
        json={
            "agents": {
                "a": {
                    "allowed_tools": ["verify_account", "get_balance"],
                    "sequences": [
                        {"before": "verify_account", "requires": ["get_balance"]},
                        {"before": "get_balance", "requires": ["verify_account"]},
                    ],
                }
            }
        },
    )
    assert resp.status_code == 422
    assert "circular prerequisite" in resp.json()["detail"]


def test_create_policy_accepts_valid_config(client):
    resp = client.post(
        "/policies",
        params={"name": "light"},
        json={"agents": {"a": {"allowed_tools": ["verify_account"], "sequences": []}}},
    )
    assert resp.status_code == 200
    assert client.get("/policies/light").status_code == 200


def test_create_policy_rejects_duplicate_name(client):
    resp = client.post(
        "/policies",
        params={"name": "strict-payments"},
        json={"agents": {}},
    )
    assert resp.status_code == 409


def test_update_policy_also_validates(client):
    resp = client.put(
        "/policies/strict-payments",
        json={"agents": {"a": {"allowed_tools": ["ghost_tool"], "sequences": []}}},
    )
    assert resp.status_code == 422


def test_get_policy_records_checkout_and_appears_in_checkouts(client):
    resp = client.get("/policies/strict-payments", headers={"X-Session-Id": "sess1", "X-Client-Id": "pipeline-a"})
    assert resp.status_code == 200
    checkouts = client.get("/checkouts").json()
    assert len(checkouts) == 1
    assert checkouts[0]["session_id"] == "sess1"
    assert checkouts[0]["client_id"] == "pipeline-a"
    assert checkouts[0]["status"] == "active"


def test_checkout_cleared_once_trace_received(client):
    client.get("/policies/strict-payments", headers={"X-Session-Id": "sess2"})
    assert len(client.get("/checkouts").json()) == 1

    trace = {
        "session_id": "sess2",
        "policy": "strict-payments",
        "started_at": "t0",
        "ended_at": "t1",
        "calls": [],
        "summary": {"total_calls": 0, "successful": 0, "blocked": 0, "response_withheld": 0, "violations_by_type": {}},
    }
    resp = client.post("/traces", json=trace)
    assert resp.status_code == 200
    assert client.get("/checkouts").json() == []
    assert client.get("/traces/sess2").status_code == 200


def test_tool_schema_export_formats(client):
    openai_resp = client.get("/tools/verify_account/schema?format=openai").json()
    assert openai_resp["type"] == "function"
    assert openai_resp["function"]["name"] == "verify_account"

    anthropic_resp = client.get("/tools/verify_account/schema?format=anthropic").json()
    assert "input_schema" in anthropic_resp

    assert client.get("/tools/nonexistent/schema").status_code == 404


# -- UI smoke tests -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/ui/policies",
        "/ui/policies/new",
        "/ui/policies/strict-payments",
        "/ui/tools",
        "/ui/activity",
        "/ui/orphans",
    ],
)
def test_ui_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_ui_policy_detail_404_for_unknown_policy(client):
    resp = client.get("/ui/policies/does-not-exist")
    assert resp.status_code == 404
