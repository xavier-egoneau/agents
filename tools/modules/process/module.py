from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.execution_sandbox import ExecutionSandbox
from agentic_kernel.platform.processes import (
    process_running,
    stop_async_process,
    subprocess_group_kwargs,
    terminate_tree,
)

MAX_CAPTURE_BYTES = 100_000
MAX_OUTPUT_BYTES = 50_000
_processes: dict[str, subprocess.Popen[bytes]] = {}
_profiles: dict[str, Path] = {}


def _success(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": metadata}


def _failure(kind: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"type": kind, "message": message},
        "metadata": metadata,
    }


def _cwd(ctx: RunContext[Any], raw: str) -> Path:
    candidate = Path(raw).expanduser()
    target = (
        (ctx.deps.workspace / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    return target


def _command(program: str, args: list[str]) -> list[str]:
    if not program.strip() or "\x00" in program:
        raise ValueError("invalid program")
    if any("\x00" in value for value in args):
        raise ValueError("invalid command argument")
    if os.name == "nt":
        resolved = shutil.which(program) or program
        if Path(resolved).suffix.casefold() in {".cmd", ".bat"}:
            if any(re.search(r"[&|<>^%!`\r\n]", value) for value in args):
                raise ValueError("unsafe metacharacter in Windows command-shim argument")
            return [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "call",
                resolved,
                *args,
            ]
        program = resolved
    return [program, *args]


def _state_db(ctx: RunContext[Any]) -> Path:
    path = ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS processes (
                process_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, pid INTEGER NOT NULL,
                command_json TEXT NOT NULL, cwd TEXT NOT NULL, output_path TEXT NOT NULL,
                started_at TEXT NOT NULL, stopped_at TEXT, return_code INTEGER
            )"""
        )
    return path


def _record(ctx: RunContext[Any], process_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(_state_db(ctx)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM processes WHERE process_id = ? AND session_id = ?",
            (process_id, str(ctx.deps.session_id)),
        ).fetchone()
    return dict(row) if row else None


def _running(pid: int) -> bool:
    return process_running(pid)


def _redacted_command(command: list[str]) -> list[str]:
    result: list[str] = []
    hide_next = False
    for value in command:
        lowered = value.casefold()
        if hide_next:
            result.append("***")
            hide_next = False
        elif any(word in lowered for word in ("token=", "password=", "api_key=", "authorization=")):
            result.append(value.split("=", 1)[0] + "=***" if "=" in value else "***")
        else:
            result.append(value)
            hide_next = lowered in {"--token", "--password", "--api-key", "--authorization"}
    return result


async def command_run(
    ctx: RunContext[Any],
    program: str,
    args: list[str] | None = None,
    cwd: str = ".",
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 120,
    network: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Run a structured command without a shell and return bounded output."""
    command = _command(program, args or [])
    workdir = _cwd(ctx, cwd)
    prepared = ExecutionSandbox.prepare(command, ctx.deps, allow_network=network)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *prepared.command,
        cwd=workdir,
        env=prepared.env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_group_kwargs(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        await stop_async_process(process, 3)
        return _failure(
            "timeout",
            f"command exceeded {timeout_seconds:g}s",
            duration_ms=(time.monotonic() - started) * 1000,
            command=_redacted_command(command),
            sandboxed=prepared.sandboxed,
            sandbox_backend=prepared.backend,
        )
    finally:
        prepared.cleanup()
    stdout_truncated = len(stdout) > MAX_CAPTURE_BYTES
    stderr_truncated = len(stderr) > MAX_CAPTURE_BYTES
    data = {
        "command": _redacted_command(command),
        "cwd": str(workdir),
        "exit_code": process.returncode,
        "stdout": stdout[:MAX_CAPTURE_BYTES].decode(errors="replace"),
        "stderr": stderr[:MAX_CAPTURE_BYTES].decode(errors="replace"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    return _success(
        data,
        duration_ms=(time.monotonic() - started) * 1000,
        bytes_captured=min(len(stdout), MAX_CAPTURE_BYTES) + min(len(stderr), MAX_CAPTURE_BYTES),
        sandboxed=prepared.sandboxed,
        sandbox_backend=prepared.backend,
    )


async def process_start(
    ctx: RunContext[Any],
    program: str,
    args: list[str] | None = None,
    cwd: str = ".",
    network: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Start a structured persistent process and store its identity durably."""
    command = _command(program, args or [])
    workdir = _cwd(ctx, cwd)
    prepared = ExecutionSandbox.prepare(command, ctx.deps, allow_network=network)
    process_id = str(uuid4())
    output_dir = ctx.deps.events.directory / "processes" / str(ctx.deps.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{process_id}.log"
    stream = output_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            prepared.command,
            cwd=workdir,
            env=prepared.env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            **subprocess_group_kwargs(),
        )
    finally:
        stream.close()
    if prepared.profile_path is not None:
        _profiles[process_id] = prepared.profile_path
    _processes[process_id] = process
    started_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(_state_db(ctx)) as db:
        db.execute(
            """INSERT INTO processes
               (process_id, session_id, pid, command_json, cwd, output_path, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                process_id,
                str(ctx.deps.session_id),
                process.pid,
                json.dumps(_redacted_command(command)),
                str(workdir),
                str(output_path),
                started_at,
            ),
        )
    await asyncio.sleep(0.15)
    return _success(
        {
            "process_id": process_id,
            "pid": process.pid,
            "running": process.poll() is None,
            "command": _redacted_command(command),
            "cwd": str(workdir),
            "started_at": started_at,
            "sandboxed": prepared.sandboxed,
            "sandbox_backend": prepared.backend,
        }
    )


async def process_status(
    ctx: RunContext[Any], process_id: str, justification: str = ""
) -> dict[str, Any]:
    """Return durable status and ports observed in recent output."""
    record = _record(ctx, process_id)
    if not record:
        return _failure("not_found", f"unknown process: {process_id}")
    process = _processes.get(process_id)
    return_code = process.poll() if process else record["return_code"]
    running = process.poll() is None if process else _running(int(record["pid"]))
    output = Path(record["output_path"]).read_text(errors="replace")[-20_000:]
    ports = sorted(
        {
            int(value)
            for value in re.findall(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})", output)
            if 0 < int(value) < 65536
        }
    )
    return _success(
        {
            "process_id": process_id,
            "pid": record["pid"],
            "running": running,
            "return_code": return_code,
            "command": json.loads(record["command_json"]),
            "cwd": record["cwd"],
            "started_at": record["started_at"],
            "ports": ports,
        }
    )


async def process_output(
    ctx: RunContext[Any],
    process_id: str,
    offset: Annotated[int, Field(ge=0)] = 0,
    max_bytes: Annotated[int, Field(ge=1, le=MAX_OUTPUT_BYTES)] = 20_000,
    justification: str = "",
) -> dict[str, Any]:
    """Read a byte range from a durable process log."""
    record = _record(ctx, process_id)
    if not record:
        return _failure("not_found", f"unknown process: {process_id}")
    path = Path(record["output_path"])
    raw = path.read_bytes() if path.exists() else b""
    chunk = raw[offset : offset + max_bytes]
    next_offset = offset + len(chunk)
    return _success(
        {
            "process_id": process_id,
            "output": chunk.decode(errors="replace"),
            "offset": offset,
            "next_offset": next_offset,
            "total_bytes": len(raw),
            "truncated": next_offset < len(raw),
        }
    )


async def process_stop(
    ctx: RunContext[Any],
    process_id: str,
    grace_seconds: Annotated[float, Field(ge=0, le=10)] = 3,
    justification: str = "",
) -> dict[str, Any]:
    """Stop a process group previously started in this session."""
    record = _record(ctx, process_id)
    if not record:
        return _failure("not_found", f"unknown process: {process_id}")
    pid = int(record["pid"])
    process = _processes.get(process_id)
    forced = False
    is_running = process.poll() is None if process else _running(pid)
    if is_running:
        forced = await asyncio.to_thread(terminate_tree, pid, grace_seconds)
        if process is not None:
            try:
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()
                forced = True
    process = _processes.pop(process_id, None)
    profile = _profiles.pop(process_id, None)
    if profile is not None:
        profile.unlink(missing_ok=True)
    return_code = process.poll() if process else None
    stopped_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(_state_db(ctx)) as db:
        db.execute(
            "UPDATE processes SET stopped_at = ?, return_code = ? WHERE process_id = ?",
            (stopped_at, return_code, process_id),
        )
    return _success(
        {
            "process_id": process_id,
            "running": False,
            "forced": forced,
            "return_code": return_code,
            "stopped_at": stopped_at,
        }
    )


class ProcessModule:
    def toolsets(self):
        return [
            FunctionToolset(
                tools=[command_run, process_start, process_status, process_output, process_stop]
            )
        ]

    def instructions(self):
        return [
            "Use command_run for finite commands. Use process_start for servers and watchers, "
            "then process_status/process_output to verify readiness and process_stop when done. "
            "Never emulate shell syntax inside arguments."
        ]

    def capabilities(self):
        return []


module = ProcessModule()
