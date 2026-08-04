from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tempfile
import tomllib
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
    def __init__(self, system: str, backend: str | None = None) -> None:
        self.capabilities = SandboxCapabilities(
            backend=backend or f"{system.lower()}-native"
        )

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


class CodexCliSandbox:
    """Use the open-source Codex CLI as a cross-platform OS sandbox helper."""

    capabilities = SandboxCapabilities(
        backend=f"codex-{platform.system().lower()}-sandbox",
        process_tree_isolation=True,
        filesystem_isolation=True,
        # Windows Firewall blocks public egress for the offline sandbox user,
        # but loopback remains reachable at the socket layer. The Guardian
        # still gates declared local/private targets, so do not overstate the
        # OS-level network guarantee here.
        network_isolation=platform.system() != "Windows",
    )

    def __init__(self, executable: str) -> None:
        self.executable = executable

    @staticmethod
    def discover() -> str | None:
        if os.getenv("AMK_CODEX_SANDBOX", "1").casefold() in {"0", "false", "no", "off"}:
            return None
        executable = shutil.which("codex")
        if executable is None:
            return None
        # Codex's unelevated Windows fallback refuses the restricted read
        # carve-outs AMK needs to hide provider and credential files. Never
        # silently weaken the profile just to make a command run.
        if platform.system() == "Windows" and not _codex_windows_elevated():
            return None
        return executable

    def prepare(
        self,
        command: list[str],
        deps: Any,
        runtime: RuntimeDirectories,
        *,
        allow_network: bool,
    ) -> PreparedExecution:
        mode = getattr(deps, "security_mode", SecurityMode.LIMITED)
        parent = ":read-only" if mode is SecurityMode.SAFE else ":workspace"
        filesystem: dict[str, str] = {
            ":root": "deny",
            ":minimal": "read",
            str(runtime.runtime_root.resolve()): "write",
            str(runtime.artifact_root.resolve()): "write",
            str(Path(sys.base_prefix).resolve()): "read",
        }
        protected = {
            runtime.events_root.parent / "providers.json",
            runtime.events_root.parent / "secrets.json",
            Path.home() / ".ssh",
            Path.home() / ".gnupg",
            Path.home() / ".aws",
            Path.home() / ".kube",
            Path.home() / ".codex",
        }
        filesystem.update(
            {str(path.resolve()): "deny" for path in protected if path.exists()}
        )
        entries = ", ".join(
            f"{json.dumps(path)}={json.dumps(permission)}"
            for path, permission in sorted(filesystem.items())
        )
        network = (
            '{enabled=true, mode="full", allow_local_binding=true}'
            if allow_network
            else "{enabled=false}"
        )
        profile = (
            f'{{ extends={json.dumps(parent)}, filesystem={{{entries}}}, '
            f"network={network} }}"
        )
        sandbox_command = [
                self.executable,
                "sandbox",
                "-C",
                str(runtime.workspace),
                "-P",
                "amk-runtime",
                "-c",
                f"permissions.amk-runtime={profile}",
            ]
        if not allow_network:
            sandbox_command.append("--sandbox-state-disable-network")
        sandbox_command.extend(["--", *command])
        return PreparedExecution(
            command=sandbox_command,
            env=runtime_environment(runtime),
            profile_path=None,
            sandboxed=True,
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
    if codex := CodexCliSandbox.discover():
        return CodexCliSandbox(codex)
    if platform.system() == "Windows" and shutil.which("codex"):
        return UnavailableSandbox("Windows", "codex-windows-unelevated-insufficient")
    if MacOSSeatbeltSandbox.available():
        return MacOSSeatbeltSandbox()
    return UnavailableSandbox(platform.system())


def sandbox_capabilities() -> SandboxCapabilities:
    return selected_backend().capabilities


def prepare_execution(
    command: list[str], deps: Any, *, allow_network: bool = False
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


def _codex_windows_elevated() -> bool:
    codex_home = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")).expanduser()
    try:
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    windows = config.get("windows")
    return isinstance(windows, dict) and windows.get("sandbox") == "elevated"
