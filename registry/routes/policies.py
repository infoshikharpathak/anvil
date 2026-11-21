"""Policy CRUD + the bundle-fetch endpoint the library calls at startup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from anvil.config import ConfigError, validate_config
from anvil.models import AnvilConfig, PolicyConfig

from ..auth import caller_app

router = APIRouter(tags=["policies"])


def _validate_against_registry(storage, policy_name: str, policy: PolicyConfig) -> None:
    """Run the same fail-fast checks the YAML loader applies (dangling tool
    references, cycles, etc.) against the registry's current tool set, so a
    policy created/edited through the API or UI can't skip them."""
    candidate = AnvilConfig(
        apps=storage.apps,
        tools=storage.list_tools(),
        # All stored policies, with the candidate swapped in: grants may name
        # agents from any policy.
        policies={**storage.list_policies(), policy_name: policy},
        grants=storage.grants,
        settings=storage.settings,
    )
    try:
        validate_config(candidate)
    except ConfigError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _require_owned(storage, app: str | None, policy: PolicyConfig, policy_name: str) -> None:
    if not storage.owns(app, policy):
        raise HTTPException(
            status_code=403,
            detail=f"Policy '{policy_name}' must contain only agents of app '{app}'",
        )


@router.get("/policies")
def list_policies(request: Request, app: str | None = Depends(caller_app)) -> dict:
    storage = request.app.state.storage
    return {
        name: policy.model_dump()
        for name, policy in storage.list_policies(app).items()
    }


@router.get("/policies/{policy_name}")
def get_policy(
    policy_name: str,
    request: Request,
    x_session_id: str | None = Header(default=None),
    x_client_id: str | None = Header(default=None),
    app: str | None = Depends(caller_app),
) -> dict:
    """Returns the full config bundle (tools + this policy + settings) that
    the library needs to run standalone. Also records a policy checkout for
    orphan-session detection, keyed by X-Session-Id."""
    storage = request.app.state.storage
    # Another app's policy is a 404, not a 403, so callers can't probe names.
    bundle = storage.get_full_config_for_policy(policy_name, app)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Policy '{policy_name}' not found")

    if x_session_id:
        storage.record_checkout(x_session_id, policy_name, x_client_id, app)

    return bundle.model_dump()


@router.post("/policies")
def create_policy(
    name: str, policy: PolicyConfig, request: Request, app: str | None = Depends(caller_app)
) -> dict:
    storage = request.app.state.storage
    if name in storage.list_policies():
        raise HTTPException(status_code=409, detail=f"Policy '{name}' already exists")
    _require_owned(storage, app, policy, name)
    _validate_against_registry(storage, name, policy)
    storage.upsert_policy(name, policy)
    return {"created": name}


@router.put("/policies/{policy_name}")
def update_policy(
    policy_name: str, policy: PolicyConfig, request: Request, app: str | None = Depends(caller_app)
) -> dict:
    storage = request.app.state.storage
    existing = storage.list_policies().get(policy_name)
    if existing is not None and not storage.owns(app, existing):
        raise HTTPException(status_code=404, detail=f"Policy '{policy_name}' not found")
    _require_owned(storage, app, policy, policy_name)
    _validate_against_registry(storage, policy_name, policy)
    storage.upsert_policy(policy_name, policy)
    return {"updated": policy_name}


@router.delete("/policies/{policy_name}")
def delete_policy(policy_name: str, request: Request, app: str | None = Depends(caller_app)) -> dict:
    storage = request.app.state.storage
    if policy_name not in storage.list_policies(app):
        raise HTTPException(status_code=404, detail=f"Policy '{policy_name}' not found")
    active = [c for c in storage.list_checkouts() if c["policy_name"] == policy_name and c["status"] == "active"]
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Policy '{policy_name}' has {len(active)} active session(s) still checked out "
                f"(e.g. {active[0]['session_id']}). Wait for them to complete or let them expire "
                f"before deleting."
            ),
        )
    storage.delete_policy(policy_name)
    return {"deleted": policy_name}
