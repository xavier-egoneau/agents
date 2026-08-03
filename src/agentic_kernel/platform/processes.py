from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil


def subprocess_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def process_running(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def listening_pids(port: int) -> list[int]:
    result: set[int] = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return []
    for connection in connections:
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and connection.laddr.port == port
            and connection.pid is not None
        ):
            result.add(connection.pid)
    return sorted(result)


def process_command(pid: int) -> str:
    try:
        return subprocess.list2cmdline(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def process_cwd(pid: int) -> Path | None:
    try:
        return Path(psutil.Process(pid).cwd()).resolve()
    except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
        return None


def process_pids() -> list[int]:
    return psutil.pids()


def process_parent_pid(pid: int) -> int | None:
    try:
        return psutil.Process(pid).ppid() or None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _tree(pid: int) -> tuple[psutil.Process | None, list[psutil.Process]]:
    try:
        parent = psutil.Process(pid)
        return parent, parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None, []


def terminate_tree(pid: int, grace_seconds: float = 3) -> bool:
    """Terminate a process and its descendants; return whether force was needed."""
    parent, children = _tree(pid)
    processes = [*children, *([parent] if parent is not None else [])]
    if not processes:
        return False
    for process in reversed(processes):
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(processes, timeout=max(0, grace_seconds))
    forced = bool(alive)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if alive:
        psutil.wait_procs(alive, timeout=3)
    return forced


async def stop_async_process(process: asyncio.subprocess.Process, grace_seconds: float = 3) -> bool:
    """Stop an asyncio process using the native process-tree implementation."""
    if process.returncode is not None:
        return False
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        try:
            await asyncio.wait_for(process.wait(), grace_seconds)
            return False
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return False
            await process.wait()
            return True
    forced = await asyncio.to_thread(terminate_tree, process.pid, grace_seconds)
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        process.kill()
        await process.wait()
        forced = True
    return forced


def wait_until_stopped(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_running(pid):
            return True
        time.sleep(0.05)
    return not process_running(pid)
