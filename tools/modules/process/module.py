from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import socket
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


def _arguments(args: list[str] | str | None) -> list[str]:
    """Normalise les arguments, qu'ils arrivent en liste ou en chaîne.

    Les modèles envoient régulièrement `"-m http.server 8080"` là où le schéma
    attend `["-m", "http.server", "8080"]`. La validation rejetait l'appel, et
    deux essais suffisaient à condamner le run entier — pour une erreur de forme
    sans ambiguïté sur l'intention.

    Le découpage passe par `shlex`, qui respecte les guillemets : un chemin
    contenant une espace reste un seul argument. Rien n'est confié à un shell,
    la commande est exécutée telle quelle.
    """
    if args is None:
        return []
    if isinstance(args, str):
        values = shlex.split(args, posix=os.name != "nt")
        if os.name == "nt":
            # `shlex(..., posix=False)` préserve correctement les antislashs
            # Windows mais conserve aussi les guillemets englobants.
            values = [
                value[1:-1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}
                else value
                for value in values
            ]
        return values
    return list(args)


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


_DEFAULT_DEV_PORTS = (3000, 5173, 8000, 8080, 8081, 4200, 4321, 5000, 9000)


async def _remove_container(cid_path: Path | None) -> None:
    """Force-remove a container whose Docker client wrapper was interrupted.

    ``command_run`` n'utilisait pas de cidfile et ne faisait pas de
    ``docker rm --force`` : le conteneur survivait à son timeout, avec ses
    ports publiés, pour tous les runs suivants.
    """
    if cid_path is None or not cid_path.is_file():
        return
    try:
        container_id = cid_path.read_text(errors="replace").strip()
        docker = shutil.which("docker")
        if docker and re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            await asyncio.to_thread(
                subprocess.run,
                [docker, "rm", "--force", container_id],
                capture_output=True,
                timeout=15,
                check=False,
            )
    except OSError:
        return


def _port_available(port: int) -> bool:
    """Return whether a loopback TCP port can safely be published."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _suggest_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _is_http_server_command(command: list[str]) -> bool:
    """Detect commands that start an HTTP server and need network access.

    Covers Python's http.server, Node/Express dev servers, Vite, webpack-dev-server,
    Next.js, Nuxt, Astro, SvelteKit dev mode, and generic ``serve``/``http-server`` CLIs.
    Without network the container starts the process but nothing can reach it,
    which is the #1 cause of "I can't show you the page" failures.
    """
    joined = " ".join(command).casefold()
    server_patterns = (
        "http.server",
        "http.server ",
        "vite ",
        "vite dev",
        "webpack serve",
        "webpack-dev-server",
        "next dev",
        "nuxt dev",
        "astro dev",
        "svelte-kit dev",
        "ng serve",
        "ember serve",
        "react-scripts start",
        "npm run dev",
        "npm run start",
        "yarn dev",
        "yarn start",
        "pnpm dev",
        "pnpm start",
        "bun run dev",
        "bun run start",
        "deno run",
        " serve ",
        " http-server ",
        "live-server",
        "browser-sync",
        "webpack serve",
    )
    return any(pattern in joined for pattern in server_patterns)


def _http_server_port(  # noqa: C901 - dette: détection de port multi-heuristiques
    command: list[str],
) -> int | None:
    """Extract the port a server command is expected to listen on.

    Looks for explicit ``-p``, ``--port``, or positional port arguments,
    then falls back to well-known defaults per tool family.
    """
    joined = " ".join(command).casefold()
    # --port=<N> or --port <N>
    import re

    match = re.search(r"(?:--port\s*[=: ]\s*)(\d{2,5})", joined)
    if match:
        return int(match.group(1))
    # -p <N>
    for i, arg in enumerate(command):
        if arg in ("-p", "--port") and i + 1 < len(command):
            try:
                return int(command[i + 1])
            except ValueError:
                pass
    # python -m http.server <N>
    if "http.server" in joined:
        for part in reversed(command):
            try:
                port = int(part)
                if 1024 <= port <= 65535:
                    return port
            except ValueError:
                pass
        return 8000
    # npx serve defaults
    if any(w in joined for w in (" serve ", "live-server", "http-server", "browser-sync")):
        for part in reversed(command):
            try:
                port = int(part)
                if 1024 <= port <= 65535:
                    return port
            except ValueError:
                pass
        return 3000
    # Framework defaults
    if "vite" in joined or "astro dev" in joined or "svelte-kit dev" in joined:
        return 5173
    if "next dev" in joined:
        return 3000
    if "ng serve" in joined:
        return 4200
    if "nuxt dev" in joined:
        return 3000
    if "react-scripts start" in joined:
        return 3000
    if "python" in joined and any(a.casefold().endswith(".py") for a in command):
        # Python script: scan args for port-like numbers
        for part in reversed(command):
            try:
                port = int(part)
                if 1024 <= port <= 65535:
                    return port
            except ValueError:
                pass
    # Fallback: scan all args for a plausible dev port
    for part in reversed(command):
        try:
            port = int(part)
            if port in _DEFAULT_DEV_PORTS:
                return port
        except ValueError:
            pass
    return None


async def command_run(  # noqa: C901 - dette: requête d'exécution multi-cas
    ctx: RunContext[Any],
    program: str,
    args: list[str] | str | None = None,
    cwd: str = ".",
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 120,
    network: bool | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Run a structured command without a shell and return bounded output."""
    command = _command(program, _arguments(args))
    if _is_http_server_command(command):
        return _failure(
            "persistent_command",
            "Cette commande lance un serveur persistant. Utilise process_start ; "
            "command_run attendrait inutilement son arrêt.",
            command=_redacted_command(command),
        )
    workdir = _cwd(ctx, cwd)
    # La détection automatique ne vaut que lorsque l'appelant n'a pas tranché.
    # `network=False` explicite est respecté : le Guardian a vu l'argument tel
    # quel, et activer le réseau derrière son dos rendait sa décision fausse.
    effective_network = (
        network if network is not None else _is_http_server_command(command)
    )
    publish_ports = None
    if effective_network:
        port = _http_server_port(command)
        if port is not None:
            publish_ports = [port]
    try:
        prepared = ExecutionSandbox.prepare(
            command,
            ctx.deps,
            allow_network=effective_network,
            publish_ports=publish_ports,
        )
    except RuntimeError as exc:
        return _failure(
            "sandbox_error",
            str(exc),
            command=_redacted_command(command),
        )
    started = time.monotonic()
    launch_command = list(prepared.command)
    cid_path: Path | None = None
    if prepared.backend == "docker" and len(launch_command) >= 2:
        # Sans cidfile, un `docker run` interrompu au timeout laisse le
        # conteneur — et ses ports publiés — vivre en arrière-plan.
        cid_path = (
            ctx.deps.events.directory
            / "processes"
            / str(ctx.deps.session_id)
            / f"{uuid4().hex}.cid"
        )
        cid_path.parent.mkdir(parents=True, exist_ok=True)
        launch_command[2:2] = ["--cidfile", str(cid_path)]
    try:
        process = await asyncio.create_subprocess_exec(
            *launch_command,
            cwd=workdir,
            env=prepared.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_group_kwargs(),
        )
    except Exception as exc:
        # Le spawn est hors du try/finally précédent : un échec laissait le
        # profil Seatbelt orphelin dans runtime/.
        prepared.cleanup()
        return _failure(
            "execution",
            f"command failed to start: {exc}",
            command=_redacted_command(command),
        )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        await stop_async_process(process, 3)
        await _remove_container(cid_path)
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
        if cid_path is not None:
            cid_path.unlink(missing_ok=True)
    stdout_truncated = len(stdout) > MAX_CAPTURE_BYTES
    stderr_truncated = len(stderr) > MAX_CAPTURE_BYTES
    stdout_text = stdout[:MAX_CAPTURE_BYTES].decode(errors="replace")
    stderr_text = stderr[:MAX_CAPTURE_BYTES].decode(errors="replace")
    # Detect common Docker sandbox failures and provide actionable messages.
    if prepared.sandboxed and process.returncode != 0:
        combined = (stdout_text + stderr_text).casefold()
        if "executable file not found" in combined or "not found" in combined:
            stderr_text = (
                f"Executable not found in sandbox container. "
                f"The command was: {command}\n"
                f"Common causes:\n"
                f"  - The program '{program}' is not installed in the sandbox image\n"
                f"  - On Windows, 'python3' may not exist (use 'python' instead)\n"
                f"  - The sandbox image may be missing tools (check AMK_SANDBOX_IMAGE)\n"
                f"Original stderr:\n{stderr_text}"
            )
        elif "permission denied" in combined:
            stderr_text = (
                f"Permission denied in sandbox. The container drops all capabilities.\n"
                f"Original stderr:\n{stderr_text}"
            )
    data = {
        "command": _redacted_command(command),
        "cwd": str(workdir),
        "exit_code": process.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    metadata = {
        "duration_ms": (time.monotonic() - started) * 1000,
        "bytes_captured": min(len(stdout), MAX_CAPTURE_BYTES) + min(len(stderr), MAX_CAPTURE_BYTES),
        "sandboxed": prepared.sandboxed,
        "sandbox_backend": prepared.backend,
    }
    if process.returncode != 0:
        return {
            "ok": False,
            "data": data,
            "error": {
                "type": "nonzero_exit",
                "message": f"command exited with code {process.returncode}",
            },
            "metadata": metadata,
        }
    return _success(data, **metadata)


async def process_start(
    ctx: RunContext[Any],
    program: str,
    args: list[str] | str | None = None,
    cwd: str = ".",
    network: bool | None = None,
    port: Annotated[int | None, Field(ge=1024, le=65535)] = None,
    justification: str = "",
) -> dict[str, Any]:
    """Start a persistent process; set port when an HTTP server must be reachable."""
    command = _command(program, _arguments(args))
    workdir = _cwd(ctx, cwd)
    # Même contrat que command_run : la détection ne vaut qu'à défaut de choix
    # explicite de l'appelant.
    effective_network = (
        network if network is not None else _is_http_server_command(command)
    )
    publish_ports = None
    if effective_network:
        detected_port = port or _http_server_port(command)
        if detected_port is not None:
            publish_ports = [detected_port]
            if not _port_available(detected_port):
                return _failure(
                    "port_in_use",
                    f"Le port {detected_port} est déjà occupé. Relance une seule fois "
                    "la même commande avec le port suggéré.",
                    port=detected_port,
                    suggested_port=_suggest_port(),
                    command=_redacted_command(command),
                )
    try:
        prepared = ExecutionSandbox.prepare(
            command,
            ctx.deps,
            allow_network=effective_network,
            publish_ports=publish_ports,
        )
    except RuntimeError as exc:
        return _failure(
            "sandbox_error",
            str(exc),
            command=_redacted_command(command),
        )
    process_id = str(uuid4())
    output_dir = ctx.deps.events.directory / "processes" / str(ctx.deps.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{process_id}.log"
    launch_command = list(prepared.command)
    cid_path = output_path.with_suffix(".cid")
    if prepared.backend == "docker" and len(launch_command) >= 2:
        # Le PID suivi est celui du client Docker. Sans cidfile, interrompre ce
        # wrapper peut laisser le vrai serveur vivre en arrière-plan et garder
        # son port occupé pour tous les runs suivants.
        launch_command[2:2] = ["--cidfile", str(cid_path)]
    stream = output_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(  # noqa: S603 - commande agent encadrée par Guardian + sandbox
            launch_command,
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
    # Ne pas annoncer « running » avant que le wrapper Docker ait eu le temps
    # d'échouer ou que le processus ait produit son premier signal de vie.
    # L'ancien délai fixe de 150 ms a validé un serveur mort (exit 127), puis le
    # navigateur a naturellement échoué à s'y connecter.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if process.poll() is not None or output_path.stat().st_size > 0:
            break
    data = {
        "process_id": process_id,
        "pid": process.pid,
        "running": process.poll() is None,
        "return_code": process.poll(),
        "command": _redacted_command(command),
        "cwd": str(workdir),
        "started_at": started_at,
        "sandboxed": prepared.sandboxed,
        "sandbox_backend": prepared.backend,
    }
    if process.poll() is not None:
        output = output_path.read_text(errors="replace")[-20_000:]
        cid_path.unlink(missing_ok=True)
        return {
            "ok": False,
            "data": {**data, "output": output},
            "error": {
                "type": "process_exited",
                "message": f"process exited during startup with code {process.returncode}",
            },
            "metadata": {},
        }
    return _success(data)


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
    cid_path = Path(record["output_path"]).with_suffix(".cid")
    if cid_path.is_file():
        container_id = cid_path.read_text(errors="replace").strip()
        docker = shutil.which("docker")
        if docker and re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            await asyncio.to_thread(
                subprocess.run,
                [docker, "rm", "--force", container_id],
                capture_output=True,
                timeout=15,
                check=False,
            )
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
    cid_path.unlink(missing_ok=True)
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
            "Never run a server with command_run. After a process_start failure, use its error "
            "and retry the same launcher at most once; do not cycle through executable aliases. "
            "Never emulate shell syntax inside arguments."
        ]

    def capabilities(self):
        return []


module = ProcessModule()
