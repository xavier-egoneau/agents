from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

MAX_READ_LINES = 2000
MAX_READ_BYTES = 50 * 1024
MAX_LIST_ENTRIES = 500
_locks: dict[str, asyncio.Lock] = {}


def _target(ctx: RunContext[Any], raw: str) -> Path:
    if not raw.strip() or "\x00" in raw:
        raise ValueError("invalid path")
    path = Path(raw).expanduser()
    return (ctx.deps.workspace / path).resolve() if not path.is_absolute() else path.resolve()


def _lock(path: Path) -> asyncio.Lock:
    return _locks.setdefault(str(path), asyncio.Lock())


async def read(
    ctx: RunContext[Any],
    path: str,
    offset: Annotated[int, Field(ge=1)] = 1,
    limit: Annotated[int, Field(ge=1, le=MAX_READ_LINES)] = MAX_READ_LINES,
    justification: str = "",
) -> dict[str, Any]:
    """Read text from a file. Offset is one-based and output is bounded."""
    target = _target(ctx, path)
    if not target.is_file():
        raise ValueError(f"not a readable file: {target}")
    raw = await asyncio.to_thread(target.read_bytes)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    selected = lines[offset - 1 : offset - 1 + limit]
    output = "".join(selected)
    encoded = output.encode("utf-8")
    byte_truncated = len(encoded) > MAX_READ_BYTES
    if byte_truncated:
        output = encoded[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
    consumed = len(output.splitlines())
    has_more = byte_truncated or offset - 1 + len(selected) < len(lines)
    return {
        "ok": True,
        "path": str(target),
        "content": output,
        "offset": offset,
        "lines_returned": consumed,
        "total_lines": len(lines),
        "truncated": has_more,
        "next_offset": offset + consumed if has_more else None,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


async def list(
    ctx: RunContext[Any],
    path: str = ".",
    limit: Annotated[int, Field(ge=1, le=MAX_LIST_ENTRIES)] = MAX_LIST_ENTRIES,
    justification: str = "",
) -> dict[str, Any]:
    """List a directory, directories first and names sorted case-insensitively."""
    target = _target(ctx, path)
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    entries = await asyncio.to_thread(lambda: sorted(
        target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold(), item.name)
    ))
    return {
        "ok": True,
        "path": str(target),
        "entries": [
            {"name": item.name, "kind": "directory" if item.is_dir() else "file"}
            for item in entries[:limit]
        ],
        "truncated": len(entries) > limit,
        "total_entries": len(entries),
    }


async def write(
    ctx: RunContext[Any], path: str, content: str, justification: str = ""
) -> dict[str, Any]:
    """Create or completely overwrite a UTF-8 text file."""
    target = _target(ctx, path)
    async with _lock(target):
        if target.exists() and not target.is_file():
            raise ValueError(f"not a file: {target}")
        before = await asyncio.to_thread(target.read_bytes) if target.exists() else b""
        encoded = content.encode("utf-8")
        if before == encoded:
            return {"ok": True, "path": str(target), "changed": False, "bytes": len(encoded)}
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(temporary.write_bytes, encoded)
            await asyncio.to_thread(os.replace, temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "ok": True, "path": str(target), "changed": True, "bytes": len(encoded),
            "before_sha256": hashlib.sha256(before).hexdigest() if before else None,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }


async def delete(ctx: RunContext[Any], path: str, justification: str = "") -> dict[str, Any]:
    """Move a file or directory to the recoverable trash for this session."""
    target = _target(ctx, path)
    async with _lock(target):
        if not target.exists():
            raise ValueError(f"path not found: {target}")
        trash = ctx.deps.events.directory / "trash" / str(ctx.deps.session_id)
        await asyncio.to_thread(trash.mkdir, parents=True, exist_ok=True)
        destination = trash / f"{uuid4().hex}-{target.name}"
        await asyncio.to_thread(shutil.move, str(target), str(destination))
        record = {
            "original_path": str(target), "trash_path": str(destination),
            "tool_call_id": str(ctx.tool_call_id), "deleted_at": datetime.now(UTC).isoformat(),
        }
        manifest = trash / "manifest.jsonl"
        await asyncio.to_thread(_append_manifest, manifest, record)
        ctx.deps.events.append(__import__("agentic_kernel.models", fromlist=["Event"]).Event(
            session_id=ctx.deps.session_id, run_id=ctx.deps.root_run_id,
            agent_id="filesystem", type="tool.trashed", payload=record,
        ))
        return {"ok": True, "recoverable": True, **record}


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class FilesystemModule:
    def toolsets(self):
        return [FunctionToolset(tools=[read, list, write, delete])]

    def instructions(self):
        return [
            "Use filesystem tools instead of shell commands. "
            "Every call must explain its justification."
        ]

    def capabilities(self):
        return []


module = FilesystemModule()
