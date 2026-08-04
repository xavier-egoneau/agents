from __future__ import annotations

import shutil
from dataclasses import dataclass

from .managed_tools import managed_executable
from .modules import ModuleRegistry
from .paths import RuntimeLayout
from .platform.sandbox import sandbox_capabilities


@dataclass(frozen=True)
class Diagnostic:
    name: str
    status: str
    detail: str
    required: bool = False


def diagnose(layout: RuntimeLayout) -> list[Diagnostic]:
    content = layout.content_root
    capabilities = sandbox_capabilities()
    checks = [
        Diagnostic("application", "ok", str(layout.application_root), True),
        Diagnostic("user data", "ok" if content.is_dir() else "missing", str(content), True),
        Diagnostic(
            "system prompt",
            "ok" if (content / "system.md").is_file() else "missing",
            str(content / "system.md"),
            True,
        ),
        Diagnostic(
            "providers",
            "ok" if (content / "providers.json").is_file() else "configure",
            str(content / "providers.json"),
        ),
        Diagnostic(
            "sandbox",
            "ok" if capabilities.execution_isolated else "limited",
            capabilities.backend,
        ),
        Diagnostic(
            "ketch",
            "ok" if shutil.which("ketch") or managed_executable("ketch") else "missing",
            shutil.which("ketch") or managed_executable("ketch") or "run amk setup",
        ),
        Diagnostic(
            "llama.cpp",
            "ok" if shutil.which("llama-server") or managed_executable("llama") else "optional",
            shutil.which("llama-server")
            or managed_executable("llama")
            or "run amk setup --full",
        ),
        Diagnostic(
            "npm development runtime",
            "ok" if shutil.which("npm") else "missing",
            shutil.which("npm") or "the development web surface currently requires npm",
            True,
        ),
    ]
    try:
        ModuleRegistry(layout.application_root / "tools").check_index()
        checks.append(Diagnostic("tool catalog", "ok", "tools/index.json", True))
    except Exception as exc:  # noqa: BLE001 - diagnostics must report every failed subsystem
        checks.append(Diagnostic("tool catalog", "error", str(exc), True))
    return checks
