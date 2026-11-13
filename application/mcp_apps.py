"""API endpoints that let MCP Apps iframes call tools through the host."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import auth, mcp_apps_host

logger = logging.getLogger("mcp_apps")

router = APIRouter(prefix="/api/mcp-apps", tags=["mcp-apps"])


class CallToolRequest(BaseModel):
    mcpServer: str = Field(..., min_length=1)
    tool: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("/call-tool")
async def call_tool(body: CallToolRequest, user: auth.User = Depends(auth.current_user)) -> dict[str, Any]:
    """Proxy `tools/call` from an MCP App iframe to in-process weather helpers."""
    try:
        return await mcp_apps_host.call_mcp_tool(
            body.mcpServer,
            body.tool,
            body.arguments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("MCP App tool call failed for %s", user.email)
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc
