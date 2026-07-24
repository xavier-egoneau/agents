from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

Scope = Literal["session", "project", "agent"]
MAX_FILE_BYTES = 1_000_000


def _db(ctx: RunContext[Any]) -> sqlite3.Connection:
    path = ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY, scope TEXT NOT NULL, scope_id TEXT NOT NULL,
            title TEXT NOT NULL, content TEXT NOT NULL, verified INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS knowledge (
            project TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
            content TEXT NOT NULL, indexed_at TEXT NOT NULL,
            PRIMARY KEY(project, path)
        )"""
    )
    return db


def _scope_id(ctx: RunContext[Any], scope: Scope, agent_id: str | None) -> str:
    if scope == "session":
        return str(ctx.deps.session_id)
    if scope == "project":
        return str(ctx.deps.workspace)
    if not agent_id:
        raise ValueError("agent_id is required for agent scope")
    return agent_id


async def memory_store(
    ctx: RunContext[Any],
    title: str,
    content: str,
    scope: Scope = "project",
    agent_id: str | None = None,
    verified: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Store an explicit memory; unverified memories are never auto-injected."""
    if not title.strip() or not content.strip():
        raise ValueError("title and content are required")
    memory_id, now = str(uuid4()), datetime.now(UTC).isoformat()
    with _db(ctx) as db:
        db.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id, scope, _scope_id(ctx, scope, agent_id), title.strip(),
                content.strip(), int(verified), now, now,
            ),
        )
    return {"ok": True, "data": {"memory_id": memory_id, "scope": scope, "verified": verified}, "error": None, "metadata": {}}


async def memory_search(
    ctx: RunContext[Any],
    query: str,
    scope: Scope = "project",
    agent_id: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    verified_only: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Search explicit memories using deterministic lexical matching."""
    with _db(ctx) as db:
        rows = db.execute(
            """SELECT memory_id, title, content, verified, updated_at FROM memories
               WHERE scope = ? AND scope_id = ?
               AND (? = 0 OR verified = 1)
               AND (lower(title) LIKE ? OR lower(content) LIKE ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (
                scope, _scope_id(ctx, scope, agent_id), int(verified_only),
                f"%{query.casefold()}%", f"%{query.casefold()}%", limit,
            ),
        ).fetchall()
    data = [{**dict(row), "content": row["content"][:2000]} for row in rows]
    return {"ok": True, "data": data, "error": None, "metadata": {"count": len(data)}}


async def memory_get(
    ctx: RunContext[Any], memory_id: str, justification: str = ""
) -> dict[str, Any]:
    """Read one explicit memory by identifier."""
    with _db(ctx) as db:
        row = db.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
    if not row:
        return {"ok": False, "data": None, "error": {"type": "not_found", "message": "memory not found"}, "metadata": {}}
    return {"ok": True, "data": dict(row), "error": None, "metadata": {}}


async def memory_forget(
    ctx: RunContext[Any], memory_id: str, justification: str = ""
) -> dict[str, Any]:
    """Delete one explicit memory."""
    with _db(ctx) as db:
        deleted = db.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,)).rowcount
    return {"ok": bool(deleted), "data": {"memory_id": memory_id, "deleted": bool(deleted)}, "error": None if deleted else {"type": "not_found", "message": "memory not found"}, "metadata": {}}


async def knowledge_index(
    ctx: RunContext[Any],
    path: str = ".",
    extensions: list[str] | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Incrementally index bounded UTF-8 project files in local SQLite."""
    root = (ctx.deps.workspace / path).resolve()
    allowed = set(extensions or [".md", ".txt", ".py", ".js", ".ts", ".tsx", ".json", ".toml", ".yaml", ".yml"])
    candidates = [root] if root.is_file() else sorted(
        (item for item in root.rglob("*") if item.is_file() and item.suffix.casefold() in allowed),
        key=lambda item: str(item).casefold(),
    )
    changed = skipped = 0
    with _db(ctx) as db:
        for candidate in candidates[:5000]:
            try:
                raw = candidate.read_bytes()
                if len(raw) > MAX_FILE_BYTES:
                    skipped += 1
                    continue
                content = raw.decode("utf-8")
            except (OSError, UnicodeError):
                skipped += 1
                continue
            digest = hashlib.sha256(raw).hexdigest()
            relative = str(candidate.relative_to(ctx.deps.workspace))
            existing = db.execute(
                "SELECT sha256 FROM knowledge WHERE project = ? AND path = ?",
                (str(ctx.deps.workspace), relative),
            ).fetchone()
            if existing and existing["sha256"] == digest:
                continue
            db.execute(
                """INSERT INTO knowledge VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project, path) DO UPDATE SET
                   sha256=excluded.sha256, content=excluded.content, indexed_at=excluded.indexed_at""",
                (str(ctx.deps.workspace), relative, digest, content, datetime.now(UTC).isoformat()),
            )
            changed += 1
    return {"ok": True, "data": {"indexed": changed, "skipped": skipped}, "error": None, "metadata": {"embedding_backend": "lexical"}}


async def knowledge_search(
    ctx: RunContext[Any],
    query: str,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    justification: str = "",
) -> dict[str, Any]:
    """Search the local project knowledge index with bounded excerpts."""
    with _db(ctx) as db:
        rows = db.execute(
            """SELECT path, content FROM knowledge
               WHERE project = ? AND lower(content) LIKE ? ORDER BY path LIMIT ?""",
            (str(ctx.deps.workspace), f"%{query.casefold()}%", limit),
        ).fetchall()
    results = []
    for row in rows:
        content, needle = row["content"], query.casefold()
        position = content.casefold().find(needle)
        start = max(0, position - 500)
        results.append({"path": row["path"], "excerpt": content[start:start + 1500]})
    return {"ok": True, "data": results, "error": None, "metadata": {"count": len(results), "backend": "lexical"}}


class MemoryModule:
    def toolsets(self):
        return [FunctionToolset(tools=[
            memory_store, memory_search, memory_get, memory_forget,
            knowledge_index, knowledge_search,
        ])]

    def instructions(self):
        return [
            "Memory is explicit: store only useful durable facts and mark verified facts. "
            "Never treat an unverified memory as authoritative."
        ]

    def capabilities(self):
        return []


module = MemoryModule()
