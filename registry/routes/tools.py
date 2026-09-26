"""Tool listing and schema export in framework-ready formats."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from anvil.models import ToolConfig

router = APIRouter(tags=["tools"])

SchemaFormat = Literal["openai", "anthropic", "raw"]


@router.get("/tools")
def list_tools(request: Request) -> dict:
    storage = request.app.state.storage
    return {name: cfg.model_dump() for name, cfg in storage.list_tools().items()}


@router.get("/tools/{tool_name}/schema")
def tool_schema(
    tool_name: str,
    request: Request,
    format: SchemaFormat = Query(default="raw"),
) -> dict[str, Any]:
    storage = request.app.state.storage
    tool: ToolConfig | None = storage.get_tool(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    params = tool.parameters or {"type": "object", "properties": {}, "required": []}

    if format == "openai":
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool.description,
                "parameters": params,
            },
        }
    if format == "anthropic":
        return {
            "name": tool_name,
            "description": tool.description,
            "input_schema": params,
        }
    # raw
    return tool.model_dump()
