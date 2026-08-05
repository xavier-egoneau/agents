from pathlib import Path

import pytest

from agentic_kernel.cli import _default_web_workspace
from agentic_kernel.paths import application_root, content_root


def test_application_root_is_found_from_an_unrelated_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMK_APP_ROOT", raising=False)
    root = Path(__file__).parents[1].resolve()

    assert application_root(Path.home()) == root


def test_existing_checkout_content_is_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AMK_HOME", raising=False)
    legacy = tmp_path / "content-agents"
    legacy.mkdir()

    assert content_root(tmp_path) == legacy


def test_fresh_install_uses_stable_user_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "amk-home"
    monkeypatch.setenv("AMK_HOME", str(home))

    assert content_root(tmp_path / "application") == home / "content-agents"


def test_web_from_a_non_project_directory_uses_a_neutral_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch_directory = tmp_path / "launch"
    launch_directory.mkdir()
    amk_home = tmp_path / "amk-home"
    monkeypatch.chdir(launch_directory)
    monkeypatch.setenv("AMK_HOME", str(amk_home))

    assert _default_web_workspace(None) == (
        amk_home / "content-agents" / "workspaces" / "main"
    )
