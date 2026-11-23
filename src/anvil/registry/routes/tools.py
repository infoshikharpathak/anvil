"""Tool listing and schema export in framework-ready formats."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from anvil.models import ToolConfig
from anvil.namespaces import DEFAULT_APP, app_of, relative, resolve

from ..auth import caller_app

router = APIRouter(tags=["tools"])

SchemaFormat = Literal["openai", "anthropic", "raw"]


@router.get("/tools")
def list_tools(request: Request, app: str | None = Depends(caller_app)) -> dict:
    storage = request.app.state.storage
    return {name: cfg.model_dump() for name, cfg in storage.list_tools(app).items()}


@router.get("/tools/{tool_name}/schema")
def tool_schema(
    tool_name: str,
    request: Request,
    format: SchemaFormat = Query(default="raw"),
    agent: str | None = Query(default=None, description="Calling agent ID; names are relative to its app"),
    app: str | None = Depends(caller_app),
) -> dict[str, Any]:
    """Export one tool's schema. Function names are relative to the calling
    app (the `agent` param's app, else the API key's app): bare for its own
    tools, "app__tool" for granted tools of other apps. `tool_name` may be
    given in any spelling execute() accepts."""
    storage = request.app.state.storage
    ctx_app = app
    if agent is not None:
        agent_id = resolve(agent, app or DEFAULT_APP)
        if app is not None and app_of(agent_id) != app:
            raise HTTPException(status_code=403, detail=f"Agent '{agent}' is not in app '{app}'")
        ctx_app = app_of(agent_id)

    tool_id = resolve(tool_name, ctx_app or DEFAULT_APP)
    tool: ToolConfig | None = storage.get_tool(tool_id, app)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    params = tool.parameters or {"type": "object", "properties": {}, "required": []}
    # OpenAI and Anthropic function names may not contain '.'. With no
    # calling app known, name the tool relative to its own app (bare).
    fn_name = relative(tool.name, ctx_app or tool.app)

    if format == "openai":
        return {
            "type": "function",
            "function": {
                "name": fn_name,
                "description": tool.description,
                "parameters": params,
            },
        }
    if format == "anthropic":
        return {
            "name": fn_name,
            "description": tool.description,
            "input_schema": params,
        }
    # raw
    return tool.model_dump()
