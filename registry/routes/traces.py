"""Trace collection, querying, and orphan-session detection."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..auth import caller_app

router = APIRouter(tags=["traces"])


@router.post("/traces")
def post_trace(
    trace: dict,
    request: Request,
    app: str | None = Depends(caller_app),
    x_client_id: str | None = Header(default=None),
) -> dict:
    storage = request.app.state.storage
    if "session_id" not in trace:
        raise HTTPException(status_code=422, detail="trace missing 'session_id'")
    if app is not None:
        policy = storage.list_policies().get(trace.get("policy", ""))
        if policy is None or not storage.owns(app, policy):
            raise HTTPException(status_code=403, detail="Trace policy does not belong to this app")
    storage.store_trace(trace, app=app, client_id=x_client_id)
    return {"stored": trace["session_id"]}


@router.get("/traces")
def list_traces(request: Request, limit: int = 50, app: str | None = Depends(caller_app)) -> list[dict]:
    storage = request.app.state.storage
    return [t.model_dump() for t in storage.list_traces(limit=limit, app=app)]


@router.get("/checkouts")
def list_checkouts(request: Request, app: str | None = Depends(caller_app)) -> list[dict]:
    """Sessions that fetched a policy but haven't posted a trace yet —
    in-flight (status=active) or past session_ttl (status=orphan)."""
    storage = request.app.state.storage
    return storage.list_checkouts(app)


@router.get("/traces/orphans")
def list_orphans(request: Request, app: str | None = Depends(caller_app)) -> list[dict]:
    """Sessions that checked out a policy but never sent a matching trace
    within the session_ttl window — crashed, disconnected, or tampered with."""
    storage = request.app.state.storage
    return [o.model_dump() for o in storage.orphan_sessions(app)]


@router.get("/traces/{session_id}")
def get_trace(session_id: str, request: Request, app: str | None = Depends(caller_app)) -> dict:
    storage = request.app.state.storage
    trace = storage.get_trace(session_id, app)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"No trace for session '{session_id}'")
    return trace.model_dump()
