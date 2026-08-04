"""Persistent, cross-platform lifecycle for locally managed llama-server processes."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from .platform.processes import listening_pids, process_command, process_running, terminate_tree

DEFAULT_PORT = 8123
_SHARD_SUFFIX = re.compile(r"-(\d{5})-of-(\d{5})$")
_PROTECTED_OPTIONS = {"-m", "--model", "--host", "--port"}

HttpStatusGet = Callable[[str, float], int]
Spawn = Callable[[list[str], Path], int]


class LlamaServerError(RuntimeError):
    """An actionable managed llama-server failure."""


@dataclass
class LlamaServerState:
    pid: int
    port: int
    model: str
    gguf_path: str
    provider_id: str
    args_hash: str
    started_at: str


def scan_gguf_models(models_dir: Path) -> list[str]:
    """Return logical GGUF names, collapsing multi-file shards."""
    if not models_dir.is_dir():
        return []
    names: set[str] = set()
    for path in models_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".gguf":
            continue
        stem = path.stem
        match = _SHARD_SUFFIX.search(stem)
        names.add(stem[: match.start()] if match else stem)
    return sorted(names)


def resolve_gguf_path(models_dir: Path, model_name: str) -> Path:
    exact = models_dir / f"{model_name}.gguf"
    if exact.is_file():
        return exact
    for shard in sorted(models_dir.glob(f"{model_name}-*-of-*.gguf")):
        match = _SHARD_SUFFIX.search(shard.stem)
        if match and match.group(1) == "00001":
            return shard
    available = ", ".join(scan_gguf_models(models_dir)) or "aucun"
    raise LlamaServerError(
        f"Modèle `{model_name}` introuvable dans {models_dir}. Modèles disponibles : {available}."
    )


class LlamaServerManager:
    """Start, reuse or swap one persistent llama-server for a provider port."""

    def __init__(
        self,
        *,
        state_dir: Path,
        models_dir: Path,
        provider_id: str,
        binary: str | None = None,
        port: int = DEFAULT_PORT,
        server_args: list[str] | None = None,
        startup_timeout_seconds: int = 180,
        http_status_get: HttpStatusGet | None = None,
        spawn: Spawn | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.models_dir = models_dir.expanduser().resolve()
        self.provider_id = provider_id
        self.binary = binary
        self.port = port
        self.server_args = list(server_args or [])
        self.startup_timeout_seconds = startup_timeout_seconds
        self._http_status_get = http_status_get or _http_status
        self._spawn = spawn or _spawn_detached

    def validate_configuration(self) -> None:
        self.resolve_binary()
        if not self.models_dir.is_dir():
            raise LlamaServerError(f"Dossier de modèles introuvable : {self.models_dir}")
        if not scan_gguf_models(self.models_dir):
            raise LlamaServerError(f"Aucun fichier GGUF dans {self.models_dir}")
        for argument in self.server_args:
            option = argument.split("=", 1)[0]
            if option in _PROTECTED_OPTIONS:
                raise LlamaServerError(
                    f"L'argument `{option}` est administré par AMK et interdit dans `llama_args`."
                )

    def ensure_running(self, model_name: str) -> LlamaServerState:
        self.validate_configuration()
        with FileLock(str(self._lock_path())).acquire(timeout=300):
            return self._ensure_running_locked(model_name)

    def status(self) -> LlamaServerState | None:
        state = self._read_state()
        return state if state and process_running(state.pid) else None

    def stop(self) -> bool:
        state = self._read_state()
        if state is None:
            return False
        stopped = False
        if process_running(state.pid) and "llama-server" in process_command(state.pid).lower():
            terminate_tree(state.pid)
            stopped = True
        self._state_path().unlink(missing_ok=True)
        return stopped

    def resolve_binary(self) -> str:
        if self.binary:
            path = Path(self.binary).expanduser().resolve()
            if path.is_file():
                return str(path)
            raise LlamaServerError(f"`server_binary` pointe vers un fichier inexistant : {path}")
        found = shutil.which("llama-server")
        if found:
            return found
        raise LlamaServerError(
            "llama-server introuvable. Ajoute-le au PATH ou renseigne `server_binary`."
        )

    def health_ok(self, timeout_seconds: float = 3.0) -> bool:
        try:
            return self._http_status_get(self._health_url(), timeout_seconds) == 200
        except OSError:
            return False

    def log_path(self) -> Path:
        return self.state_dir / "logs" / f"llama-server-{self.port}.log"

    def _ensure_running_locked(self, model_name: str) -> LlamaServerState:
        gguf = resolve_gguf_path(self.models_dir, model_name)
        argv = self._build_argv(self.resolve_binary(), gguf)
        args_hash = hashlib.sha256("\0".join(argv).encode()).hexdigest()[:16]
        state = self._read_state()
        if state is not None:
            alive = process_running(state.pid)
            is_llama = alive and "llama-server" in process_command(state.pid).lower()
            if is_llama and state.model == model_name and state.args_hash == args_hash:
                if self.health_ok():
                    return state
            if is_llama:
                terminate_tree(state.pid)
            self._state_path().unlink(missing_ok=True)

        listeners = listening_pids(self.port)
        foreign = [pid for pid in listeners if "llama-server" not in process_command(pid).lower()]
        if foreign:
            raise LlamaServerError(
                f"Le port {self.port} est occupé par un autre processus (pid "
                f"{', '.join(map(str, foreign))})."
            )
        for pid in listeners:
            terminate_tree(pid)

        pid = self._spawn(argv, self.log_path())
        state = LlamaServerState(
            pid=pid,
            port=self.port,
            model=model_name,
            gguf_path=str(gguf),
            provider_id=self.provider_id,
            args_hash=args_hash,
            started_at=datetime.now(UTC).isoformat(),
        )
        self._write_state(state)
        self._wait_ready(pid)
        return state

    def _wait_ready(self, pid: int) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if not process_running(pid):
                self._state_path().unlink(missing_ok=True)
                raise LlamaServerError(
                    f"llama-server a quitté pendant le chargement. Log : {self.log_path()}\n"
                    f"{self._log_tail()}"
                )
            if self.health_ok(2):
                return
            time.sleep(0.5)
        terminate_tree(pid)
        self._state_path().unlink(missing_ok=True)
        raise LlamaServerError(
            f"llama-server n'est pas prêt après {self.startup_timeout_seconds}s. "
            f"Vérifie {self.log_path()}\n{self._log_tail()}"
        )

    def _build_argv(self, binary: str, gguf: Path) -> list[str]:
        return [
            binary,
            "-m",
            str(gguf),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--jinja",
            *self.server_args,
        ]

    def _health_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/health"

    def _state_path(self) -> Path:
        return self.state_dir / f"llama-server-{self.port}.json"

    def _lock_path(self) -> Path:
        path = self.state_dir / "locks" / f"llama-server-{self.port}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _read_state(self) -> LlamaServerState | None:
        try:
            return LlamaServerState(**json.loads(self._state_path().read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_state(self, state: LlamaServerState) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")

    def _log_tail(self, lines: int = 30) -> str:
        try:
            content = self.log_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(log indisponible)"
        return "\n".join(content.splitlines()[-lines:])


def _http_status(url: str, timeout_seconds: float) -> int:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _spawn_detached(argv: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab")  # noqa: SIM115 - inherited by the child
    try:
        kwargs: dict[str, object] = {
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(argv, **kwargs).pid  # type: ignore[arg-type]
    finally:
        log_handle.close()
