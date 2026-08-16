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
MAX_SEARCH_MATCHES = 500
_locks: dict[str, asyncio.Lock] = {}


def _success(payload: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "data": payload,
        "error": None,
        "metadata": metadata,
    }


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
    refresh: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Read text from a file. Set refresh only to repeat an unchanged prior read."""
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
    return _success(
        {
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
    )


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
    entries = await asyncio.to_thread(
        lambda: sorted(
            target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold(), item.name)
        )
    )
    return _success(
        {
            "ok": True,
            "path": str(target),
            "entries": [
                {"name": item.name, "kind": "directory" if item.is_dir() else "file"}
                for item in entries[:limit]
            ],
            "truncated": len(entries) > limit,
            "total_entries": len(entries),
        }
    )


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
            return _success({"path": str(target), "changed": False, "bytes": len(encoded)})
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(temporary.write_bytes, encoded)
            await asyncio.to_thread(os.replace, temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return _success(
            {
                "path": str(target),
                "changed": True,
                "bytes": len(encoded),
                "before_sha256": hashlib.sha256(before).hexdigest() if before else None,
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )


async def patch(
    ctx: RunContext[Any],
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Replace an exact text fragment in an existing UTF-8 file."""
    if not old_text:
        raise ValueError("old_text must not be empty")
    target = _target(ctx, path)
    async with _lock(target):
        if not target.is_file():
            raise ValueError(f"not a file: {target}")
        before = await asyncio.to_thread(target.read_text, encoding="utf-8")
        count = before.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        if count > 1 and not replace_all:
            raise ValueError(f"old_text is ambiguous: {count} matches")
        after = before.replace(old_text, new_text, -1 if replace_all else 1)
        encoded = after.encode("utf-8")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(temporary.write_bytes, encoded)
            await asyncio.to_thread(os.replace, temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return _success(
            {
                "path": str(target),
                "changed": True,
                "replacements": count if replace_all else 1,
                "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )


async def move(
    ctx: RunContext[Any], path: str, destination: str, justification: str = ""
) -> dict[str, Any]:
    """Move one file or directory to an exact destination."""
    source, target = _target(ctx, path), _target(ctx, destination)
    async with _lock(source), _lock(target):
        if not source.exists():
            raise ValueError(f"path not found: {source}")
        if target.exists():
            raise ValueError(f"destination already exists: {target}")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(source), str(target))
        return _success({"path": str(source), "destination": str(target)})


async def copy(
    ctx: RunContext[Any], path: str, destination: str, justification: str = ""
) -> dict[str, Any]:
    """Copy one file or directory to an exact destination."""
    source, target = _target(ctx, path), _target(ctx, destination)
    async with _lock(target):
        if not source.exists():
            raise ValueError(f"path not found: {source}")
        if target.exists():
            raise ValueError(f"destination already exists: {target}")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        if source.is_dir():
            await asyncio.to_thread(shutil.copytree, source, target)
        else:
            await asyncio.to_thread(shutil.copy2, source, target)
        return _success({"path": str(source), "destination": str(target)})


async def mkdir(
    ctx: RunContext[Any], path: str, parents: bool = True, justification: str = ""
) -> dict[str, Any]:
    """Create a directory."""
    target = _target(ctx, path)
    existed = target.exists()
    if existed and not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    await asyncio.to_thread(target.mkdir, parents=parents, exist_ok=True)
    return _success({"path": str(target), "created": not existed})


async def stat(ctx: RunContext[Any], path: str, justification: str = "") -> dict[str, Any]:
    """Inspect deterministic metadata for a filesystem path."""
    target = _target(ctx, path)
    if not target.exists():
        raise ValueError(f"path not found: {target}")
    info = await asyncio.to_thread(target.stat)
    return _success(
        {
            "path": str(target),
            "kind": "directory" if target.is_dir() else "file",
            "bytes": info.st_size,
            "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
        }
    )


async def search_text(
    ctx: RunContext[Any],
    query: str,
    path: str = ".",
    limit: Annotated[int, Field(ge=1, le=MAX_SEARCH_MATCHES)] = 100,
    justification: str = "",
) -> dict[str, Any]:
    """Search literal text recursively in UTF-8 files with bounded results."""
    if not query:
        raise ValueError("query must not be empty")
    root = _target(ctx, path)
    candidates = [root] if root.is_file() else _search_candidates(root)
    candidates = [item for item in candidates if not _is_sensitive_path(item)]
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(matches) >= limit:
            break
        try:
            if candidate.stat().st_size > 2 * 1024 * 1024:
                continue
            content = await asyncio.to_thread(candidate.read_text, encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if query in line:
                matches.append(
                    {
                        "path": str(candidate),
                        "line": number,
                        "preview": line[:500],
                    }
                )
                if len(matches) >= limit:
                    break
    return _success(
        {
            "query": query,
            "matches": matches,
            "truncated": len(matches) >= limit,
        }
    )


def _search_candidates(root: Path) -> list[Path]:
    """Enumerate searchable files without following or failing on npm shims."""
    candidates: list[Path] = []
    excluded_directories = {
        ".git", ".codex", ".venv", "node_modules", "__pycache__",
        ".ssh", ".gnupg", ".aws", ".kube",
    }
    for current, directories, names in os.walk(
        root, topdown=True, onerror=lambda _error: None, followlinks=False
    ):
        directories[:] = sorted(
            (name for name in directories if name not in excluded_directories),
            key=str.casefold,
        )
        current_path = Path(current)
        for name in sorted(names, key=str.casefold):
            candidate = current_path / name
            try:
                if candidate.is_file() and not _is_sensitive_path(candidate):
                    candidates.append(candidate)
            except OSError:
                continue
    return candidates


def _is_sensitive_path(path: Path) -> bool:
    return path.name in {
        ".env",
        ".env.local",
        ".envrc",
        "providers.json",
        "secrets.json",
    } or bool(set(path.parts) & {".ssh", ".gnupg", ".aws", ".kube", ".git", ".codex"})


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
            "original_path": str(target),
            "trash_path": str(destination),
            "tool_call_id": str(ctx.tool_call_id),
            "deleted_at": datetime.now(UTC).isoformat(),
        }
        manifest = trash / "manifest.jsonl"
        await asyncio.to_thread(_append_manifest, manifest, record)
        ctx.deps.events.append(
            __import__("agentic_kernel.models", fromlist=["Event"]).Event(
                session_id=ctx.deps.session_id,
                run_id=ctx.deps.root_run_id,
                agent_id="filesystem",
                type="tool.trashed",
                payload=record,
            )
        )
        return _success({"recoverable": True, **record})


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class FilesystemModule:
    def toolsets(self):
        return [
            FunctionToolset(
                tools=[
                    read,
                    list,
                    write,
                    patch,
                    move,
                    copy,
                    mkdir,
                    stat,
                    search_text,
                    delete,
                ]
            )
        ]

    def instructions(self):
        return [
            "Use filesystem tools instead of shell commands. "
            "Every call must explain its justification."
        ]

    def capabilities(self):
        return []


module = FilesystemModule()
