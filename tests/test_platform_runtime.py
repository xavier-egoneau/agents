from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import agentic_kernel.platform.sandbox as sandbox_module
from agentic_kernel.models import SecurityMode
from agentic_kernel.platform.sandbox import (
    DockerSandbox,
    MacOSSeatbeltSandbox,
    ensure_docker_ready,
    prepare_execution,
    runtime_directories,
    runtime_environment,
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


def test_power_mode_still_uses_docker_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DockerSandbox, "discover", staticmethod(lambda: "docker"))
    monkeypatch.setattr(DockerSandbox, "image_available", lambda self: (True, "available"))

    prepared = prepare_execution(["example"], _deps(tmp_path, SecurityMode.POWER))

    assert prepared.sandboxed is True
    assert prepared.backend == "docker"
    assert prepared.command[0] == "docker"


def test_limited_mode_still_uses_docker_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DockerSandbox, "discover", staticmethod(lambda: "docker"))
    monkeypatch.setattr(DockerSandbox, "image_available", lambda self: (True, "available"))

    prepared = prepare_execution(["example"], _deps(tmp_path, SecurityMode.LIMITED))

    assert prepared.sandboxed is True
    assert prepared.backend == "docker"
    assert prepared.command[0] == "docker"


def test_power_mode_never_falls_back_to_native_when_docker_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AMK_DOCKER_SANDBOX", raising=False)
    monkeypatch.setattr(DockerSandbox, "discover", staticmethod(lambda: None))
    monkeypatch.setattr(
        DockerSandbox,
        "status",
        staticmethod(lambda: (None, "Docker daemon is not ready")),
    )

    with pytest.raises(RuntimeError, match="Docker sandbox unavailable"):
        prepare_execution(["example"], _deps(tmp_path, SecurityMode.POWER))


def test_application_start_launches_docker_desktop_and_waits_for_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter(
        [
            (None, "daemon stopped"),
            (None, "daemon starting"),
            ("docker.exe", "docker 28.0"),
        ]
    )
    launched: list[list[str]] = []
    monkeypatch.delenv("AMK_DOCKER_SANDBOX", raising=False)
    monkeypatch.setattr(DockerSandbox, "status", staticmethod(lambda: next(statuses)))
    monkeypatch.setattr(sandbox_module, "_docker_client_path", lambda: "docker.exe")
    monkeypatch.setattr(
        sandbox_module, "_docker_desktop_command", lambda: ["Docker Desktop.exe"]
    )
    monkeypatch.setattr(
        sandbox_module.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command),
    )
    monkeypatch.setattr(sandbox_module.time, "sleep", lambda seconds: None)

    ready, started, detail = ensure_docker_ready(timeout_seconds=5)

    assert (ready, started) == (True, True)
    assert detail == "docker 28.0"
    assert launched == [["Docker Desktop.exe"]]


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


def test_published_ports_are_bound_to_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--publish 8137:8137` lie sur toutes les interfaces : un dev server
    « local » devenait visible du LAN. Le loopback de l'hôte suffit."""
    deps = _deps(tmp_path, SecurityMode.LIMITED)
    runtime = runtime_directories(deps)

    bac = DockerSandbox("docker", image="image:test")
    monkeypatch.setattr(bac, "image_available", lambda: (True, ""))
    prepared = bac.prepare(
        ["example"], deps, runtime, allow_network=True, publish_ports=[8137]
    )

    assert "--publish" in prepared.command
    assert prepared.command[prepared.command.index("--publish") + 1] == "127.0.0.1:8137:8137"


def test_the_container_is_offline_and_stripped_of_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ce que le conteneur ne peut pas faire compte plus que ce qu'il exécute.

    Sans `--cap-drop=ALL` ni `--network=none`, un conteneur reste une machine
    complète avec une sortie réseau : l'isolation du système de fichiers seule
    laisserait passer l'exfiltration.
    """
    deps = _deps(tmp_path, SecurityMode.LIMITED)
    runtime = runtime_directories(deps)

    bac = DockerSandbox("docker", image="image:test")
    monkeypatch.setattr(bac, "image_available", lambda: (True, ""))
    prepared = bac.prepare(
        ["example", "arg"], deps, runtime, allow_network=False
    )

    assert "--cap-drop=ALL" in prepared.command
    assert "--security-opt=no-new-privileges" in prepared.command
    assert "--network=none" in prepared.command
    assert prepared.command[-3:] == ["image:test", "example", "arg"]
    assert prepared.sandboxed is True


def test_the_workspace_is_mounted_read_only_in_safe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _deps(tmp_path, SecurityMode.SAFE)
    runtime = runtime_directories(deps)

    bac = DockerSandbox("docker", image="image:test")
    monkeypatch.setattr(bac, "image_available", lambda: (True, ""))
    prepared = bac.prepare(
        ["example"], deps, runtime, allow_network=True
    )

    montage = next(item for item in prepared.command if "target=/workspace" in item)
    assert montage.endswith(",readonly")
    # Le réseau demandé explicitement reste ouvert : c'est le Guardian qui a
    # tranché en amont, le backend n'a pas à le rejuger.
    assert "--network=bridge" in prepared.command


def test_container_paths_replace_host_paths_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Réutiliser les chemins de l'hôte donnerait des variables qui ne
    désignent rien une fois la frontière franchie."""
    deps = _deps(tmp_path, SecurityMode.LIMITED)
    runtime = runtime_directories(deps)

    bac = DockerSandbox("docker", image="image:test")
    monkeypatch.setattr(bac, "image_available", lambda: (True, ""))
    prepared = bac.prepare(
        ["example"], deps, runtime, allow_network=False
    )

    variables = dict(
        item.split("=", 1)
        for index, item in enumerate(prepared.command)
        if index > 0 and prepared.command[index - 1] == "--env"
    )
    assert variables["HOME"] == "/tmp"
    assert variables["XDG_CACHE_HOME"] == "/cache"
    assert str(tmp_path) not in " ".join(f"{k}={v}" for k, v in variables.items())


def test_a_missing_client_and_a_stopped_daemon_are_told_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Les deux remèdes n'ont rien à voir.

    « Absent du PATH » se règle en rouvrant un terminal, « démon muet » en
    démarrant Docker. Un diagnostic qui dit seulement « pas d'isolation » laisse
    chercher au mauvais endroit — c'est ce qui s'est produit.
    """
    monkeypatch.delenv("AMK_DOCKER_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox_module, "_docker_client_path", lambda: None)

    assert "PATH" in DockerSandbox.status()[1]

    monkeypatch.setattr(sandbox_module, "_docker_client_path", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="cannot connect to the Docker daemon"
        ),
    )

    executable, raison = DockerSandbox.status()
    assert executable is None
    assert "démarrer Docker Desktop" in raison
    assert "cannot connect" in raison


def test_the_backend_can_be_turned_off_without_touching_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMK_DOCKER_SANDBOX", "0")

    assert DockerSandbox.discover() is None
