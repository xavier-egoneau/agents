from __future__ import annotations

import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SecurityMode

_SAFE_ENV = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TERM",
    "TMPDIR",
}


@dataclass(frozen=True)
class PreparedExecution:
    command: list[str]
    env: dict[str, str]
    profile_path: Path | None
    sandboxed: bool

    def cleanup(self) -> None:
        if self.profile_path is not None:
            self.profile_path.unlink(missing_ok=True)


class ExecutionSandbox:
    """Prepare a process with a filtered environment and a macOS Seatbelt profile."""

    @staticmethod
    def available() -> bool:
        return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None

    @classmethod
    def prepare(
        cls,
        command: list[str],
        deps: Any,
        *,
        allow_network: bool = True,
    ) -> PreparedExecution:
        workspace = Path(deps.workspace).resolve()
        session_id = str(getattr(deps, "session_id", "standalone"))
        events_root = Path(deps.events.directory).resolve()
        runtime_root = events_root / "runtime" / session_id
        runtime_root.mkdir(parents=True, exist_ok=True)
        home = runtime_root / "home"
        temporary = runtime_root / "tmp"
        cache = runtime_root / "cache"
        for directory in (home, temporary, cache):
            directory.mkdir(parents=True, exist_ok=True)

        env = {
            key: value
            for key, value in os.environ.items()
            if key in _SAFE_ENV or key.startswith("LC_")
        }
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "XDG_CACHE_HOME": str(cache),
                "NPM_CONFIG_CACHE": str(cache / "npm"),
                "PIP_CACHE_DIR": str(cache / "pip"),
                "NO_COLOR": "1",
            }
        )
        if not cls.available():
            mode = getattr(deps, "security_mode", SecurityMode.LIMITED)
            if mode in {SecurityMode.SAFE, SecurityMode.LIMITED}:
                raise PermissionError(
                    "execution sandbox unavailable; safe and limited modes refuse execution"
                )
            return PreparedExecution(command, env, None, False)

        protected = {
            events_root.parent / "providers.json",
            events_root.parent / "secrets.json",
            Path.home() / ".ssh",
            Path.home() / ".gnupg",
            Path.home() / ".aws",
            Path.home() / ".kube",
            Path.home() / ".codex",
        }
        artifact_root = events_root / "artifacts" / session_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow sysctl-read)",
            # Dependencies and runtimes remain readable, while writes are
            # restricted to the exact workspace and kernel-owned run roots.
            "(allow file-read*)",
        ]
        for path in sorted({workspace, runtime_root, artifact_root}):
            lines.append(f'(allow file-write* (subpath "{_seatbelt(path)}"))')
        for path in sorted(protected):
            lines.append(f'(deny file-read* file-write* (subpath "{_seatbelt(path)}"))')
        if allow_network:
            lines.append("(allow network*)")

        fd, raw_path = tempfile.mkstemp(
            prefix="amk-seatbelt-",
            suffix=".sb",
            dir=runtime_root,
            text=True,
        )
        profile_path = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        profile_path.chmod(0o600)
        wrapped = ["/usr/bin/sandbox-exec", "-f", str(profile_path), *command]
        return PreparedExecution(wrapped, env, profile_path, True)


def _seatbelt(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
