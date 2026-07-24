from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext


def _path(ctx: RunContext[Any]) -> Path:
    return ctx.deps.events.directory.parent / "models-infos.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "models": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("models"), list):
        raise ValueError("invalid content-agents/models-infos.json")
    return data


async def model_context_status(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """Read the stored context window for the exact active provider and model."""
    provider_id, model = ctx.deps.provider_id, ctx.deps.model_name
    data = _load(_path(ctx))
    entry = next(
        (
            item
            for item in data["models"]
            if item.get("provider_id") == provider_id and item.get("model") == model
        ),
        None,
    )
    return {
        "ok": True,
        "data": {
            "provider_id": provider_id,
            "model": model,
            "known": entry is not None,
            "context_window_tokens": (entry.get("context_window_tokens") if entry else None),
            "source": entry.get("source") if entry else None,
        },
        "error": None,
        "metadata": {},
    }


async def model_context_store(
    ctx: RunContext[Any],
    context_window_tokens: Annotated[int, Field(ge=1024, le=10_000_000)],
    source: str = "user-confirmed",
    justification: str = "",
) -> dict[str, Any]:
    """Persist a confirmed context window for the exact active model."""
    provider_id, model = ctx.deps.provider_id, ctx.deps.model_name
    if not provider_id or not model:
        raise ValueError("the active provider and model are not resolved")
    path = _path(ctx)
    data = _load(path)
    now = datetime.now(UTC).isoformat()
    entry = {
        "provider_id": provider_id,
        "model": model,
        "context_window_tokens": context_window_tokens,
        "source": source.strip()[:300] or "user-confirmed",
        "updated_at": now,
    }
    models = [
        item
        for item in data["models"]
        if not (item.get("provider_id") == provider_id and item.get("model") == model)
    ]
    models.append(entry)
    data["models"] = sorted(models, key=lambda item: (item["provider_id"], item["model"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    ctx.deps.context_window_tokens = context_window_tokens
    return {"ok": True, "data": entry, "error": None, "metadata": {"path": str(path)}}


class ModelInfoModule:
    def toolsets(self):
        return [FunctionToolset(tools=[model_context_status, model_context_store])]

    def instructions(self):
        return [
            "The kernel compacts at 70% of the stored model context window. "
            "Never guess an unknown window; follow the model-context skill."
        ]

    def capabilities(self):
        return []


module = ModelInfoModule()
