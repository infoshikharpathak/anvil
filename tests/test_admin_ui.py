"""Registry UI admin auth: required once app keys exist, open otherwise."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anvil.config import load_config_dict
from registry.app import app as registry_app
from registry.auth import hash_api_key
from registry.storage import RegistryStorage

ADMIN_KEY = "adm_test_5f0c2b7e9a"
APP_KEY = "ik_test_ironclad_0123456789"
UI_PAGES = ["/", "/ui/policies", "/ui/policies/new", "/ui/policies/ironclad-banking", "/ui/tools",
            "/ui/activity", "/ui/orphans"]


def _raw(with_keys=True):
    return {
        "apps": {
            "ironclad": {
                "base_url": "http://bank",
                "api_keys": {"ironclad-agent": hash_api_key(APP_KEY)} if with_keys else {},
                "tools": {"get_accounts": {"path": "/a", "trust": "trusted"}},
            },
            "notify": {"base_url": "http://n", "tools": {"send_email": {"path": "/e"}}},
        },
        "policies": {
            "ironclad-banking": {"agents": {"ironclad.banking_agent": {"allowed_tools": ["get_accounts"]}}},
            "notify-ops": {"agents": {"notify.dispatcher": {"allowed_tools": ["send_email"]}}},
        },
    }


def make(with_keys=True, admin=True):
    registry_app.state.storage = RegistryStorage(
        load_config_dict(_raw(with_keys)), admin_key_digest=hash_api_key(ADMIN_KEY) if admin else None
    )
    return TestClient(registry_app)


@pytest.mark.parametrize("path", UI_PAGES)
def test_ui_requires_admin_when_app_keys_exist(path):
    c = make()
    resp = c.get(path)
    assert resp.status_code == 401
    assert 'id="login-form"' in resp.text  # browsers get the login page
    assert c.get(path, headers={"Authorization": f"Bearer {APP_KEY}", "X-Client-Id": "ironclad-agent"}).status_code == 401
    assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get(path, headers={"Authorization": f"Bearer {ADMIN_KEY}"}).status_code == 200


@pytest.mark.parametrize("path", UI_PAGES)
def test_ui_open_without_app_keys(path):
    assert make(with_keys=False, admin=False).get(path).status_code == 200


def test_login_page_reachable_and_rejects_bad_key():
    c = make()
    assert c.get("/ui/login").status_code == 200
    assert c.post("/ui/login", json={"key": "nope"}).status_code == 401
    assert c.post("/ui/login", json={"key": APP_KEY}).status_code == 401
    assert "anvil_admin_session" not in c.cookies


def test_ui_flow_login_then_policy_save_and_create():
    c = make()
    resp = c.post("/ui/login", json={"key": ADMIN_KEY})
    assert resp.status_code == 200
    assert ADMIN_KEY not in resp.headers["set-cookie"]  # cookie holds a session token, not the key
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert "samesite=strict" in resp.headers["set-cookie"].lower()

    # pages now render, admin sees every app's policies
    page = c.get("/ui/policies")
    assert page.status_code == 200 and "notify-ops" in page.text

    # what postJSON() sends: the session cookie plus X-Anvil-UI
    ui = {"X-Anvil-UI": "1", "Content-Type": "application/json"}
    edited = {"description": "edited", "agents": {"notify.dispatcher": {"allowed_tools": ["send_email"]}}}
    assert c.put("/policies/notify-ops", json=edited, headers=ui).status_code == 200
    new = {"agents": {"ironclad.helper": {"allowed_tools": ["get_accounts"]}}}
    assert c.post("/policies", params={"name": "fresh"}, json=new, headers=ui).status_code == 200
    assert c.get("/policies/notify-ops", headers=ui).json()["policies"]["notify-ops"]["description"] == "edited"
    # activity polling goes through the same helper
    assert c.get("/checkouts", headers=ui).status_code == 200

    # the cookie alone (e.g. a cross-site form post) is not enough for the API
    assert c.put("/policies/notify-ops", json=edited).status_code == 401

    c.post("/ui/logout")
    assert c.get("/ui/policies").status_code == 401


def test_admin_bearer_key_is_unscoped_on_api():
    c = make()
    got = c.get("/policies", headers={"Authorization": f"Bearer {ADMIN_KEY}"}).json()
    assert set(got) == {"ironclad-banking", "notify-ops"}


def test_app_keys_without_admin_key_lock_the_ui():
    c = make(admin=False)
    resp = c.get("/ui/policies")
    assert resp.status_code == 401 and "ANVIL_ADMIN_KEY" in resp.text


def test_admin_digest_must_not_be_plaintext():
    with pytest.raises(ValueError, match="SHA-256 hex digest"):
        RegistryStorage(load_config_dict(_raw()), admin_key_digest=ADMIN_KEY)


def test_ui_fetch_helpers_send_admin_credential():
    import registry

    js = (Path(registry.__file__).parent / "static" / "app.js").read_text()  # the installed copy
    post_json = js[js.index("async function postJSON"):]
    assert "apiFetch(url" in post_json[:200]
    api_fetch = js[js.index("function apiFetch"):js.index("async function postJSON")]
    assert '"X-Anvil-UI": "1"' in api_fetch and 'credentials: "same-origin"' in api_fetch
    assert "fetch(\"/checkouts\")" not in js and "apiFetch(\"/checkouts\")" in js
