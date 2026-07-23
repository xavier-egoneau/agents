from pathlib import Path

import pytest

from agentic_kernel import web_launcher
from agentic_kernel.errors import ConfigurationError


def test_stops_only_owned_amk_processes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        web_launcher,
        "listening_pids",
        lambda port: [101] if port == 8765 else [202],
    )
    monkeypatch.setattr(web_launcher, "is_amk_process", lambda *args: True)
    stopped: list[int] = []
    monkeypatch.setattr(web_launcher, "_terminate", stopped.append)
    assert web_launcher.stop_previous_instances(tmp_path, 8765, 3000) == [101, 202]
    assert stopped == [101, 202]


def test_refuses_to_kill_unrelated_listener(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        web_launcher,
        "listening_pids",
        lambda port: [303] if port == 3000 else [],
    )
    monkeypatch.setattr(web_launcher, "is_amk_process", lambda *args: False)
    monkeypatch.setattr(web_launcher, "process_command", lambda pid: "another-server")
    with pytest.raises(ConfigurationError, match="refusing to stop non-AMK"):
        web_launcher.stop_previous_instances(tmp_path, 8765, 3000)


def test_identifies_backend_and_frontend_by_command_and_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    commands = {1: ".venv/bin/python .venv/bin/amk serve", 2: "node bin/vinext dev"}
    directories = {1: tmp_path, 2: tmp_path / "surfaces" / "web"}
    monkeypatch.setattr(web_launcher, "process_command", commands.get)
    monkeypatch.setattr(web_launcher, "process_cwd", directories.get)
    assert web_launcher.is_amk_process(1, tmp_path, 8765, 8765, 3000)
    assert web_launcher.is_amk_process(2, tmp_path, 3000, 8765, 3000)
