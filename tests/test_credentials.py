"""Credential injection: Anvil adds the app's auth header from its env var,
and the secret never appears in traces, audit logs, corrections, or data."""

import json
import logging

import httpx
import pytest

from anvil.client import Anvil
from anvil.config import load_config_dict
from anvil.models import ViolationType

SECRET = "sk-test-7f3a9c1e5b2d4f6a8c0e"

RAW = {
    "apps": {
        "ironclad": {
            "base_url": "http://bank",
            "auth": {"header": "Authorization", "secret_env": "IRONCLAD_TOKEN", "prefix": "Bearer "},
            "tools": {
                "echo": {"path": "/echo", "trust": "trusted"},
                "echo_untrusted": {"path": "/echo", "trust": "untrusted"},
                "fail": {"path": "/fail", "trust": "trusted"},
            },
        },
        "public": {"base_url": "http://pub", "tools": {"ping": {"path": "/ping", "trust": "trusted"}}},
    },
    "policies": {
        "p": {
            "agents": {
                "ironclad.agent": {"allowed_tools": ["echo", "echo_untrusted", "fail"]},
                "public.agent": {"allowed_tools": ["ping"]},
            }
        }
    },
}


class Upstream:
    """Echoes request headers back in the body, like a debugging endpoint."""

    def __init__(self):
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/fail":
            return httpx.Response(500, text=f"boom {request.headers.get('authorization')}")
        return httpx.Response(
            200,
            json={
                "headers": dict(request.headers),
                "nested": [{"note": f"token was {SECRET}"}],
                SECRET: "secret as a key",
            },
        )


def make(upstream):
    client = Anvil(load_config_dict(RAW), "p", session_id="cred-session")
    client._transport = httpx.MockTransport(upstream)
    return client


@pytest.mark.asyncio
async def test_auth_header_injected_from_env(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", SECRET)
    up = Upstream()
    resp = await make(up).execute("ironclad.agent", "echo", {"q": 1})
    assert resp.success
    assert up.seen[0].headers["authorization"] == f"Bearer {SECRET}"


@pytest.mark.asyncio
async def test_app_without_auth_sends_no_auth_header(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", SECRET)
    up = Upstream()
    await make(up).execute("public.agent", "ping", {})
    assert "authorization" not in up.seen[0].headers


@pytest.mark.asyncio
async def test_secret_never_leaks(monkeypatch, capsys, caplog):
    monkeypatch.setenv("IRONCLAD_TOKEN", SECRET)
    caplog.set_level(logging.DEBUG)
    client = make(Upstream())

    responses = [
        await client.execute("ironclad.agent", "echo", {}),
        await client.execute("ironclad.agent", "echo_untrusted", {}),
        await client.execute("ironclad.agent", "fail", {}),
    ]
    assert responses[0].success and responses[1].success
    assert responses[2].correction.violation == ViolationType.UPSTREAM_ERROR
    assert responses[0].data["headers"]["authorization"] == "Bearer [REDACTED]"

    trace = client.trace.build_payload()
    surfaces = {
        "returned data and corrections": json.dumps([r.model_dump(mode="json") for r in responses]),
        "trace": json.dumps(trace),
        "audit stdout": capsys.readouterr().out,
        "logs": caplog.text,
        "instance state": repr(vars(client)),
    }
    for name, text in surfaces.items():
        assert SECRET not in text, f"secret leaked into {name}"
    # the audit logger writes through caplog here; make sure we actually saw it
    assert "anvil.audit" in caplog.text and "cred-session" in caplog.text


@pytest.mark.asyncio
async def test_missing_secret_fails_closed_without_calling_upstream(monkeypatch):
    monkeypatch.delenv("IRONCLAD_TOKEN", raising=False)
    up = Upstream()
    resp = await make(up).execute("ironclad.agent", "echo", {})
    assert resp.success is False and resp.tool_executed is False
    assert resp.correction.violation == ViolationType.UPSTREAM_ERROR
    assert "IRONCLAD_TOKEN" in resp.correction.message
    assert up.seen == []


@pytest.mark.asyncio
async def test_non_json_response_is_upstream_error(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", SECRET)
    client = make(lambda req: httpx.Response(200, text=f"not json {SECRET}"))
    resp = await client.execute("ironclad.agent", "echo", {})
    assert resp.correction.violation == ViolationType.UPSTREAM_ERROR
    assert SECRET not in resp.correction.message
