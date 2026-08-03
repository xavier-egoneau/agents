from __future__ import annotations

import os
import platform
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agentic_kernel.models import SecurityMode
from agentic_kernel.platform.sandbox import (
    MacOSSeatbeltSandbox,
    prepare_execution,
    runtime_directories,
    runtime_environment,
    sandbox_capabilities,
)


def _deps(tmp_path: Path, mode: SecurityMode = SecurityMode.POWER):
    sessions = tmp_path / "content-agents" / "sessions"
    sessions.mkdir(parents=True)
    return SimpleNamespace(
        workspace=tmp_path,
        session_id=uuid4(),
        security_mode=mode,
        events=SimpleNamespace(directory=sessions),
    )


def test_runtime_environment_uses_session_owned_directories(tmp_path: Path) -> None:
    runtime = runtime_directories(_deps(tmp_path))
    env = runtime_environment(runtime)
    assert env["HOME"] == str(runtime.home)
    assert env["TMPDIR"] == str(runtime.temporary)
    assert env["NPM_CONFIG_CACHE"].startswith(str(runtime.cache))
    if os.name == "nt":
        assert env["USERPROFILE"] == str(runtime.home)
        assert env["TEMP"] == str(runtime.temporary)
        assert env["TMP"] == str(runtime.temporary)


def test_native_backend_reports_isolation_truthfully(tmp_path: Path) -> None:
    prepared = prepare_execution(["example"], _deps(tmp_path))
    capabilities = sandbox_capabilities()
    assert prepared.sandboxed is capabilities.execution_isolated
    assert prepared.backend == capabilities.backend
    if platform.system() != "Darwin":
        assert capabilities.filesystem_isolation is False


def test_seatbelt_profile_preserves_existing_macos_policy(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    runtime = runtime_directories(deps)
    prepared = MacOSSeatbeltSandbox().prepare(
        ["echo", "ok"], deps, runtime, allow_network=False
    )
    try:
        assert prepared.profile_path is not None
        profile = prepared.profile_path.read_text(encoding="utf-8")
        assert "(deny default)" in profile
        assert "(allow process*)" in profile
        assert "(allow network*)" not in profile
        assert str(runtime.workspace).replace("\\", "\\\\") in profile
        assert prepared.command[-2:] == ["echo", "ok"]
    finally:
        prepared.cleanup()
