from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext


def _result(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": metadata}


async def tool_search(
    ctx: RunContext[Any],
    query: str,
    limit: Annotated[int, Field(ge=1, le=20)] = 8,
    justification: str = "",
) -> dict[str, Any]:
    """Search tools using deterministic token matching over the current index."""
    tokens = {token.casefold() for token in query.replace("_", " ").split() if token}
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for item in ctx.deps.tool_catalog:
        haystack = " ".join(
            str(item.get(key, "")) for key in ("name", "description", "category", "module")
        ).casefold()
        score = sum(
            3 if token in str(item.get("name", "")).casefold() else 1
            for token in tokens
            if token in haystack
        )
        if not tokens or score:
            summary = {
                "name": item["name"],
                "module": item["module"],
                "description": item["description"],
                "category": item.get("category", "general"),
                "risk_tags": item.get("risk_tags", []),
            }
            ranked.append((score, str(item["name"]), summary))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    matches = [item for _, _, item in ranked[:limit]]
    return _result(matches, query=query, total_matches=len(ranked), truncated=len(ranked) > limit)


async def tool_describe(ctx: RunContext[Any], name: str, justification: str = "") -> dict[str, Any]:
    """Return the full indexed descriptor for an exact tool name."""
    for item in ctx.deps.tool_catalog:
        if item.get("name") == name:
            return _result(item)
    return {
        "ok": False,
        "data": None,
        "error": {"type": "not_found", "message": f"unknown tool: {name}"},
        "metadata": {},
    }


class CatalogModule:
    def toolsets(self):
        return [FunctionToolset(tools=[tool_search, tool_describe])]

    def instructions(self):
        return [
            "Use tool_search when no currently known tool clearly matches the task. "
            "Use tool_describe before calling an unfamiliar tool."
        ]

    def capabilities(self):
        return []


module = CatalogModule()
