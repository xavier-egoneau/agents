from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.managed_tools import discovered_codegraph_executable
from agentic_kernel.models import Event

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 90


def _failure(kind: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"type": kind, "message": message},
        "metadata": metadata,
    }


def _executable() -> str | None:
    return discovered_codegraph_executable()


async def _run(
    ctx: RunContext[Any],
    arguments: list[str],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT,
    prepare: bool = True,
) -> dict[str, Any]:
    executable = _executable()
    workspace = ctx.deps.workspace.resolve()
    if executable is None:
        return _failure(
            "dependency_missing",
            "CodeGraph CLI absent. Installe `@colbymchenry/codegraph` puis réessaie.",
            workspace=str(workspace),
        )
    env = {**os.environ, "CODEGRAPH_TELEMETRY": "0", "DO_NOT_TRACK": "1"}
    if prepare:
        command = "sync" if (workspace / ".codegraph").is_dir() else "init"
        prepared = await _subprocess([executable, command, "."], workspace, timeout_seconds, env)
        if isinstance(prepared, dict):
            return prepared
        if prepared.returncode != 0:
            return _failure(
                "index_failed",
                f"CodeGraph {command} exited {prepared.returncode}.",
                stdout=prepared.stdout[:4000],
                stderr=prepared.stderr[:4000],
                returncode=prepared.returncode,
            )
    completed = await _subprocess([executable, *arguments], workspace, timeout_seconds, env)
    if isinstance(completed, dict):
        return completed
    stdout, stderr = completed.stdout or "", completed.stderr or ""
    artifacts = []
    if len(stdout) > MAX_OUTPUT_CHARS:
        artifacts.append(_artifact(ctx, "codegraph-output.txt", stdout))
    if len(stderr) > MAX_OUTPUT_CHARS:
        artifacts.append(_artifact(ctx, "codegraph-error.txt", stderr))
    data = {
        "command": arguments[0],
        "stdout": stdout[:MAX_OUTPUT_CHARS],
        "stderr": stderr[:MAX_OUTPUT_CHARS],
        "returncode": completed.returncode,
        "stdout_truncated": len(stdout) > MAX_OUTPUT_CHARS,
        "stderr_truncated": len(stderr) > MAX_OUTPUT_CHARS,
        "artifacts": artifacts,
    }
    return {
        "ok": completed.returncode == 0,
        "data": data if completed.returncode == 0 else None,
        "error": (
            None
            if completed.returncode == 0
            else {"type": "execution", "message": f"CodeGraph exited {completed.returncode}."}
        ),
        "metadata": {
            "workspace": str(workspace),
            "index": str(workspace / ".codegraph"),
        },
    }


async def _subprocess(
    argv: list[str], workspace: Path, timeout: int, env: dict[str, str]
) -> subprocess.CompletedProcess[str] | dict[str, Any]:
    try:
        return await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return _failure(
            "timeout",
            f"CodeGraph timed out after {timeout}s.",
            stdout=_text(exc.stdout)[:4000],
            stderr=_text(exc.stderr)[:4000],
        )
    except OSError as exc:
        return _failure("execution", str(exc))


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _artifact(ctx: RunContext[Any], name: str, content: str) -> dict[str, Any]:
    artifact_id = uuid4().hex
    root = ctx.deps.events.directory / "artifacts" / str(ctx.deps.session_id) / artifact_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(content, encoding="utf-8")
    payload = {
        "artifact_id": artifact_id,
        "name": name,
        "media_type": "text/plain",
        "kind": "file",
        "bytes": path.stat().st_size,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    ctx.deps.events.append(
        Event(
            session_id=ctx.deps.session_id,
            run_id=ctx.deps.root_run_id,
            agent_id="kernel",
            type="artifact.created",
            payload=payload,
        )
    )
    return {key: payload[key] for key in ("artifact_id", "name", "bytes", "sha256")}


async def codegraph_explore(
    ctx: RunContext[Any],
    query: str,
    timeout_seconds: Annotated[int, Field(ge=1, le=180)] = DEFAULT_TIMEOUT,
    justification: str = "",
) -> dict[str, Any]:
    """Return relevant source, relationships and impact for one semantic question."""
    if not query.strip():
        return _failure("validation", "query is required")
    return await _run(ctx, ["explore", query.strip()], timeout_seconds=timeout_seconds)


async def codegraph_query(
    ctx: RunContext[Any],
    search: str,
    kind: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    justification: str = "",
) -> dict[str, Any]:
    """Search indexed symbols by name or text."""
    arguments = ["query", search.strip(), "--limit", str(limit), "--json"]
    if kind:
        arguments.extend(["--kind", kind])
    return await _run(ctx, arguments)


async def codegraph_node(
    ctx: RunContext[Any], symbol_or_file: str, justification: str = ""
) -> dict[str, Any]:
    """Read one indexed symbol or file with its relationships."""
    return await _run(ctx, ["node", symbol_or_file.strip()])


async def codegraph_callers(
    ctx: RunContext[Any],
    symbol: str,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
    justification: str = "",
) -> dict[str, Any]:
    """List callers of one indexed symbol."""
    return await _run(ctx, ["callers", symbol.strip(), "--limit", str(limit), "--json"])


async def codegraph_callees(
    ctx: RunContext[Any],
    symbol: str,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
    justification: str = "",
) -> dict[str, Any]:
    """List symbols called by one indexed symbol."""
    return await _run(ctx, ["callees", symbol.strip(), "--limit", str(limit), "--json"])


async def codegraph_impact(
    ctx: RunContext[Any],
    symbol: str,
    depth: Annotated[int, Field(ge=1, le=10)] = 3,
    justification: str = "",
) -> dict[str, Any]:
    """Compute the bounded impact radius of changing one symbol."""
    return await _run(ctx, ["impact", symbol.strip(), "--depth", str(depth), "--json"])


async def codegraph_affected(
    ctx: RunContext[Any],
    files: Annotated[list[str], Field(min_length=1, max_length=100)],
    depth: Annotated[int, Field(ge=1, le=10)] = 5,
    justification: str = "",
) -> dict[str, Any]:
    """Find tests affected by a bounded list of changed workspace files."""
    return await _run(ctx, ["affected", *files, "--depth", str(depth), "--json"])


async def codegraph_status(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """Read index statistics and staleness without rebuilding it."""
    return await _run(ctx, ["status", ".", "--json"], prepare=False)


async def codegraph_sync(
    ctx: RunContext[Any],
    full: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Explicitly synchronize or fully rebuild the technical project index."""
    command = (
        "index" if full else ("sync" if (ctx.deps.workspace / ".codegraph").is_dir() else "init")
    )
    arguments = [command, "."]
    if full:
        arguments.append("--force")
    return await _run(ctx, arguments, prepare=False, timeout_seconds=180)


class CodeGraphModule:
    def toolsets(self):
        return [
            FunctionToolset(
                tools=[
                    codegraph_explore,
                    codegraph_query,
                    codegraph_node,
                    codegraph_callers,
                    codegraph_callees,
                    codegraph_impact,
                    codegraph_affected,
                    codegraph_status,
                    codegraph_sync,
                ]
            )
        ]

    def instructions(self):
        return [
            "Use codegraph_explore for architecture and flow questions. "
            "CodeGraph results are navigation evidence; heed stale-index warnings "
            "and read current source before editing."
        ]

    def capabilities(self):
        return []


module = CodeGraphModule()
