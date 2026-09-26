"""Trace collection, querying, and orphan-session detection."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["traces"])


@router.post("/traces")
def post_trace(trace: dict, request: Request) -> dict:
    storage = request.app.state.storage
    if "session_id" not in trace:
        raise HTTPException(status_code=422, detail="trace missing 'session_id'")
    storage.store_trace(trace)
    return {"stored": trace["session_id"]}


@router.get("/traces")
def list_traces(request: Request, limit: int = 50) -> list[dict]:
    storage = request.app.state.storage
    return [t.model_dump() for t in storage.list_traces(limit=limit)]


@router.get("/traces/orphans")
def list_orphans(request: Request) -> list[dict]:
    """Sessions that checked out a policy but never sent a matching trace
    within the session_ttl window — crashed, disconnected, or tampered with."""
    storage = request.app.state.storage
    return [o.model_dump() for o in storage.orphan_sessions()]


@router.get("/traces/{session_id}")
def get_trace(session_id: str, request: Request) -> dict:
    storage = request.app.state.storage
    trace = storage.get_trace(session_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"No trace for session '{session_id}'")
    return trace.model_dump()
