from pathlib import Path

import pytest

from agentic_kernel import web_launcher
from agentic_kernel.errors import ConfigurationError


def install_processes(monkeypatch, commands: dict[int, str], parents: dict[int, int | None]):
    monkeypatch.setattr(web_launcher.os, "getpid", lambda: 500)
    monkeypatch.setattr(web_launcher, "process_pids", lambda: list(commands))
    monkeypatch.setattr(web_launcher, "process_command", lambda pid: commands.get(pid, ""))
    monkeypatch.setattr(web_launcher, "process_parent_pid", lambda pid: parents.get(pid))


def test_stops_other_web_launcher_regardless_of_project(monkeypatch) -> None:
    install_processes(
        monkeypatch,
        {
            100: "uv run amk web --web-port 4100",
            101: "C:\\other-project\\.venv\\Scripts\\amk.EXE web --web-port 4100",
            500: "C:\\current-project\\.venv\\Scripts\\amk.EXE web",
            900: "another-server",
        },
        {100: 1, 101: 100, 500: 499, 499: 1, 900: 1},
    )
    stopped: list[int] = []
    monkeypatch.setattr(web_launcher, "_terminate", stopped.append)

    assert web_launcher.stop_previous_instances() == [100]
    assert stopped == [100]


def test_never_stops_current_launcher_or_its_parents(monkeypatch) -> None:
    install_processes(
        monkeypatch,
        {499: "uv run amk web", 500: "amk.exe web"},
        {500: 499, 499: 1},
    )
    stopped: list[int] = []
    monkeypatch.setattr(web_launcher, "_terminate", stopped.append)

    assert web_launcher.stop_previous_instances() == []
    assert stopped == []


def test_refuses_to_kill_standalone_kernel_on_api_port(monkeypatch) -> None:
    monkeypatch.setattr(web_launcher, "listening_pids", lambda port: [303])
    monkeypatch.setattr(web_launcher, "process_command", lambda pid: "amk serve")

    with pytest.raises(ConfigurationError, match="hors surface Web"):
        web_launcher.ensure_api_port_available(8765)


def test_selects_next_web_port_without_stopping_unrelated_app(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        web_launcher,
        "listening_pids",
        lambda port: [303] if port == 3000 else [],
    )

    assert web_launcher.available_web_port(tmp_path, 3000, 8765) == 3001


@pytest.mark.parametrize(
    "command",
    [
        "uv run amk web",
        "C:\\project\\.venv\\Scripts\\amk.EXE web --api-port 9000",
        "python -m agentic_kernel.cli web",
    ],
)
def test_identifies_web_launcher_commands(command: str, monkeypatch) -> None:
    monkeypatch.setattr(web_launcher, "process_command", lambda pid: command)
    assert web_launcher.is_amk_web_launcher(1)


def test_does_not_identify_standalone_kernel(monkeypatch) -> None:
    monkeypatch.setattr(web_launcher, "process_command", lambda pid: "amk serve")
    assert not web_launcher.is_amk_web_launcher(1)


def test_does_not_identify_shell_that_only_mentions_command(monkeypatch) -> None:
    monkeypatch.setattr(
        web_launcher,
        "process_command",
        lambda pid: 'pwsh.exe -Command "Write-Host uv run amk web"',
    )
    assert not web_launcher.is_amk_web_launcher(1)
