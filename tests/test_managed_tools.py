from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentic_kernel.cli import app
from agentic_kernel.managed_tools import ManagedToolInstaller, discovered_executable


def test_release_asset_selection_is_exact() -> None:
    release = {
        "assets": [
            {"name": "tool_linux_x86_64.tar.gz"},
            {"name": "tool_windows_x86_64.zip"},
        ]
    }

    selected = ManagedToolInstaller._asset(release, "_windows_x86_64")

    assert selected["name"] == "tool_windows_x86_64.zip"


def test_setup_without_downloads_creates_user_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMK_HOME", str(tmp_path / "home"))

    result = CliRunner().invoke(app, ["setup", "--no-downloads"])

    assert result.exit_code == 0, result.output
    content = tmp_path / "home" / "content-agents"
    assert (content / "system.md").is_file()
    assert (content / "agents" / "main.md").is_file()


def test_discovery_accepts_an_existing_configured_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "ketch.exe"
    binary.write_bytes(b"")
    monkeypatch.setenv("AMK_KETCH_BIN", str(binary))

    assert discovered_executable("ketch", "AMK_KETCH_BIN") == str(binary.resolve())
