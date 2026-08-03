from __future__ import annotations

import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentic_kernel.models import SecurityMode
from agentic_kernel.platform.secure_files import secure_file

_SAFE_ENV = {
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TERM",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


@dataclass(frozen=True)
class SandboxCapabilities:
    backend: str
    process_tree_isolation: bool = False
    filesystem_isolation: bool = False
    network_isolation: bool = False

    @property
    def execution_isolated(self) -> bool:
        return self.process_tree_isolation and self.filesystem_isolation


@dataclass(frozen=True)
class PreparedExecution:
    command: list[str]
    env: dict[str, str]
    profile_path: Path | None
    sandboxed: bool
    backend: str

    def cleanup(self) -> None:
        if self.profile_path is not None:
            self.profile_path.unlink(missing_ok=True)


class SandboxBackend(Protocol):
    capabilities: SandboxCapabilities

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
    ) -> PreparedExecution: ...


@dataclass(frozen=True)
class RuntimeDirectories:
    workspace: Path
    events_root: Path
    runtime_root: Path
    home: Path
    temporary: Path
    cache: Path
    artifact_root: Path


class UnavailableSandbox:
    def __init__(self, system: str) -> None:
        self.capabilities = SandboxCapabilities(backend=f"{system.lower()}-native")

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
    ) -> PreparedExecution:
        del allow_network
        mode = getattr(deps, "security_mode", SecurityMode.LIMITED)
        if mode in {SecurityMode.SAFE, SecurityMode.LIMITED}:
            raise PermissionError(
                f"execution sandbox unavailable on {platform.system()}; "
                "safe and limited modes refuse native execution"
            )
        return PreparedExecution(
            command=command,
            env=runtime_environment(runtime),
            profile_path=None,
            sandboxed=False,
            backend=self.capabilities.backend,
        )


class MacOSSeatbeltSandbox:
    capabilities = SandboxCapabilities(
        backend="macos-seatbelt",
        process_tree_isolation=True,
        filesystem_isolation=True,
        network_isolation=True,
    )

    @staticmethod
    def available() -> bool:
        return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
    ) -> PreparedExecution:
        del deps
        protected = {
            runtime.events_root.parent / "providers.json",
            runtime.events_root.parent / "secrets.json",
            Path.home() / ".ssh",
            Path.home() / ".gnupg",
            Path.home() / ".aws",
            Path.home() / ".kube",
            Path.home() / ".codex",
        }
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow sysctl-read)",
            "(allow file-read*)",
        ]
        writable = {runtime.workspace, runtime.runtime_root, runtime.artifact_root}
        for path in sorted(writable, key=str):
            lines.append(f'(allow file-write* (subpath "{_seatbelt(path)}"))')
        for path in sorted(protected, key=str):
            lines.append(f'(deny file-read* file-write* (subpath "{_seatbelt(path)}"))')
        if allow_network:
            lines.append("(allow network*)")

        fd, raw_path = tempfile.mkstemp(
            prefix="amk-seatbelt-",
            suffix=".sb",
            dir=runtime.runtime_root,
            text=True,
        )
        profile_path = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        secure_file(profile_path)
        return PreparedExecution(
            command=["/usr/bin/sandbox-exec", "-f", str(profile_path), *command],
            env=runtime_environment(runtime),
            profile_path=profile_path,
            sandboxed=True,
            backend=self.capabilities.backend,
        )


def selected_backend() -> SandboxBackend:
    if MacOSSeatbeltSandbox.available():
        return MacOSSeatbeltSandbox()
    return UnavailableSandbox(platform.system())


def sandbox_capabilities() -> SandboxCapabilities:
    return selected_backend().capabilities


def prepare_execution(
    command: list[str], deps: Any, *, allow_network: bool = True
) -> PreparedExecution:
    runtime = runtime_directories(deps)
    return selected_backend().prepare(
        command,
        deps,
        runtime,
        allow_network=allow_network,
    )


def runtime_directories(deps: Any) -> RuntimeDirectories:
    workspace = Path(deps.workspace).resolve()
    session_id = str(getattr(deps, "session_id", "standalone"))
    events_root = Path(deps.events.directory).resolve()
    runtime_root = events_root / "runtime" / session_id
    home = runtime_root / "home"
    temporary = runtime_root / "tmp"
    cache = runtime_root / "cache"
    artifact_root = events_root / "artifacts" / session_id
    for directory in (home, temporary, cache, artifact_root):
        directory.mkdir(parents=True, exist_ok=True)
    return RuntimeDirectories(
        workspace=workspace,
        events_root=events_root,
        runtime_root=runtime_root,
        home=home,
        temporary=temporary,
        cache=cache,
        artifact_root=artifact_root,
    )


def runtime_environment(runtime: RuntimeDirectories) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_ENV or key.startswith("LC_")
    }
    env.update(
        {
            "HOME": str(runtime.home),
            "TMPDIR": str(runtime.temporary),
            "XDG_CACHE_HOME": str(runtime.cache),
            "NPM_CONFIG_CACHE": str(runtime.cache / "npm"),
            "PIP_CACHE_DIR": str(runtime.cache / "pip"),
            "NO_COLOR": "1",
        }
    )
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(runtime.home))
        env.update(
            {
                "USERPROFILE": str(runtime.home),
                "HOMEDRIVE": drive,
                "HOMEPATH": tail or "\\",
                "TEMP": str(runtime.temporary),
                "TMP": str(runtime.temporary),
                "LOCALAPPDATA": str(runtime.cache / "local"),
                "APPDATA": str(runtime.cache / "roaming"),
            }
        )
    return env


def _seatbelt(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
