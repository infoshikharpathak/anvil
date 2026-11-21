"""Server-rendered UI for the Anvil Registry (v2).

Jinja2 templates, no build step. Interactive bits (policy save/create,
activity polling) call the existing JSON API via fetch() from app.js —
this module never duplicates that logic, it only renders pages and reads
from storage directly for initial page data.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

from ..auth import SESSION_COOKIE, require_admin_ui

# Pages need the admin login once app keys exist (see registry/auth.py).
router = APIRouter(include_in_schema=False, dependencies=[Depends(require_admin_ui)])
# Login/logout must stay reachable without being logged in.
login_router = APIRouter(include_in_schema=False)


def login_page(request: Request, status_code: int = 200):
    keys = request.app.state.storage.keys
    return templates.TemplateResponse(
        request,
        "login.html",
        {"active": "", "admin_configured": keys.admin_digest is not None},
        status_code=status_code,
    )


@login_router.get("/ui/login", response_class=HTMLResponse)
def login_form(request: Request):
    return login_page(request)


@login_router.post("/ui/login")
def login(request: Request, key: str = Body(..., embed=True)):
    keys = request.app.state.storage.keys
    if not keys.is_admin_key(key):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE,
        keys.new_admin_session(),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return resp


@login_router.post("/ui/logout")
def logout(request: Request):
    request.app.state.storage.keys.end_admin_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

POLICY_SKELETON = {
    "description": "",
    "agents": {
        "example_agent": {
            "allowed_tools": [],
            "allowed_delegations": [],
            "sequences": [],
            "strictness": "strict",
            "trust_overrides": {},
        }
    },
}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    storage = request.app.state.storage
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "policies": storage.list_policies(),
            "tools": storage.list_tools(),
            "traces": storage.list_traces(limit=50),
            "active_checkouts": len([c for c in storage.list_checkouts() if c["status"] == "active"]),
            "orphans": len(storage.orphan_sessions()),
        },
    )


@router.get("/ui/policies", response_class=HTMLResponse)
def policies_list(request: Request):
    storage = request.app.state.storage
    return templates.TemplateResponse(
        request, "policies_list.html", {"active": "policies", "policies": storage.list_policies()}
    )


@router.get("/ui/policies/new", response_class=HTMLResponse)
def policy_new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "policy_new.html",
        {"active": "policies", "skeleton_json": json.dumps(POLICY_SKELETON, indent=2)},
    )


@router.get("/ui/policies/{policy_name}", response_class=HTMLResponse)
def policy_detail(policy_name: str, request: Request):
    storage = request.app.state.storage
    policy = storage.list_policies().get(policy_name)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"Policy '{policy_name}' not found")
    return templates.TemplateResponse(
        request,
        "policy_detail.html",
        {
            "active": "policies",
            "policy_name": policy_name,
            "policy": policy,
            "policy_json": json.dumps(policy.model_dump(), indent=2),
        },
    )


@router.get("/ui/tools", response_class=HTMLResponse)
def tools_list(request: Request):
    storage = request.app.state.storage
    return templates.TemplateResponse(request, "tools_list.html", {"active": "tools", "tools": storage.list_tools()})


@router.get("/ui/activity", response_class=HTMLResponse)
def activity(request: Request):
    return templates.TemplateResponse(request, "activity.html", {"active": "activity"})


@router.get("/ui/orphans", response_class=HTMLResponse)
def orphans(request: Request):
    storage = request.app.state.storage
    return templates.TemplateResponse(
        request, "orphans.html", {"active": "orphans", "orphans": storage.orphan_sessions()}
    )


@router.get("/ui/traces/{session_id}", response_class=HTMLResponse)
def trace_detail(session_id: str, request: Request):
    storage = request.app.state.storage
    trace = storage.get_trace(session_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"No trace for session '{session_id}'")
    return templates.TemplateResponse(
        request, "trace_detail.html", {"active": "activity", "trace": trace.model_dump()}
    )
