from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext


def _config(ctx: RunContext[Any]) -> dict[str, dict[str, Any]]:
    path = ctx.deps.events.directory.parent / "mcp.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("servers", data)
    if isinstance(servers, list):
        return {item["id"]: item for item in servers}
    return dict(servers) if isinstance(servers, dict) else {}


def _failure(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"type": kind, "message": message}, "metadata": {}}


async def _rpc(ctx: RunContext[Any], server_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    server = _config(ctx).get(server_id)
    if not server:
        raise ValueError(f"unknown MCP server: {server_id}")
    if server.get("transport", "http") not in {"http", "streamable-http"}:
        raise ValueError("only configured HTTP MCP transports are supported here")
    url = server.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("MCP server URL must be configured as HTTP(S)")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    credential_env = server.get("credential_env")
    if credential_env:
        token = os.getenv(credential_env)
        if not token:
            raise PermissionError(f"credential reference unavailable: {credential_env}")
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid4()),
        "method": method,
        "params": params,
    }
    async with httpx.AsyncClient(timeout=float(server.get("timeout_seconds", 60))) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
    if body.get("error"):
        raise RuntimeError(str(body["error"])[:2000])
    return body.get("result", {})


async def mcp_search(
    ctx: RunContext[Any],
    query: str,
    server_id: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    justification: str = "",
) -> dict[str, Any]:
    """Search tool names and descriptions from configured MCP servers."""
    servers = [server_id] if server_id else sorted(_config(ctx))
    results = []
    try:
        for current in servers:
            listing = await _rpc(ctx, current, "tools/list", {})
            for tool in listing.get("tools", []):
                haystack = f"{tool.get('name', '')} {tool.get('description', '')}".casefold()
                if query.casefold() in haystack:
                    results.append({
                        "server_id": current,
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                    })
                    if len(results) >= limit:
                        break
    except (ValueError, PermissionError, RuntimeError, httpx.HTTPError, json.JSONDecodeError) as exc:
        return _failure(type(exc).__name__, str(exc))
    return {"ok": True, "data": results, "error": None, "metadata": {"count": len(results)}}


async def mcp_describe(
    ctx: RunContext[Any],
    server_id: str,
    tool_name: str,
    justification: str = "",
) -> dict[str, Any]:
    """Load one MCP tool schema on demand."""
    try:
        listing = await _rpc(ctx, server_id, "tools/list", {})
        tool = next((item for item in listing.get("tools", []) if item.get("name") == tool_name), None)
    except (ValueError, PermissionError, RuntimeError, httpx.HTTPError, json.JSONDecodeError) as exc:
        return _failure(type(exc).__name__, str(exc))
    if not tool:
        return _failure("not_found", f"unknown MCP tool: {server_id}/{tool_name}")
    return {"ok": True, "data": tool, "error": None, "metadata": {"server_id": server_id}}


async def mcp_call(
    ctx: RunContext[Any],
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    justification: str = "",
) -> dict[str, Any]:
    """Call one configured MCP tool with structured arguments."""
    try:
        result = await _rpc(
            ctx, server_id, "tools/call", {"name": tool_name, "arguments": arguments}
        )
    except (ValueError, PermissionError, RuntimeError, httpx.HTTPError, json.JSONDecodeError) as exc:
        return _failure(type(exc).__name__, str(exc))
    encoded = json.dumps(result, ensure_ascii=False)
    truncated = len(encoded.encode()) > 200_000
    if truncated:
        result = {"content": encoded[:200_000]}
    return {"ok": True, "data": result, "error": None, "metadata": {"truncated": truncated}}


class McpModule:
    def toolsets(self):
        return [FunctionToolset(tools=[mcp_search, mcp_describe, mcp_call])]

    def instructions(self):
        return [
            "Discover MCP tools with mcp_search and mcp_describe before mcp_call. "
            "Connections and credentials come only from content-agents/mcp.json."
        ]

    def capabilities(self):
        return []


module = McpModule()
