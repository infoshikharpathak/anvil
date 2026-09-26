"""Policy CRUD + the bundle-fetch endpoint the library calls at startup."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from anvil.models import PolicyConfig

router = APIRouter(tags=["policies"])


@router.get("/policies")
def list_policies(request: Request) -> dict:
    storage = request.app.state.storage
    return {
        name: policy.model_dump()
        for name, policy in storage.list_policies().items()
    }


@router.get("/policies/{policy_name}")
def get_policy(
    policy_name: str,
    request: Request,
    x_session_id: str | None = Header(default=None),
    x_client_id: str | None = Header(default=None),
) -> dict:
    """Returns the full config bundle (tools + this policy + settings) that
    the library needs to run standalone. Also records a policy checkout for
    orphan-session detection, keyed by X-Session-Id."""
    storage = request.app.state.storage
    bundle = storage.get_full_config_for_policy(policy_name)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Policy '{policy_name}' not found")

    if x_session_id:
        storage.record_checkout(x_session_id, policy_name, x_client_id)

    return bundle.model_dump()


@router.post("/policies")
def create_policy(name: str, policy: PolicyConfig, request: Request) -> dict:
    storage = request.app.state.storage
    if name in storage.list_policies():
        raise HTTPException(status_code=409, detail=f"Policy '{name}' already exists")
    storage.upsert_policy(name, policy)
    return {"created": name}


@router.put("/policies/{policy_name}")
def update_policy(policy_name: str, policy: PolicyConfig, request: Request) -> dict:
    storage = request.app.state.storage
    storage.upsert_policy(policy_name, policy)
    return {"updated": policy_name}
