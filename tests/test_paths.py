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


def test_pointing_at_an_existing_content_directory_is_understood(tmp_path) -> None:
    """Le réglage désigne le parent, l'écran parle du contenu.

    Pointer sur un `content-agents` existant est donc le geste naturel, et
    produisait `…/content-agents/content-agents` — une installation vide, à
    côté des données réelles.
    """
    from agentic_kernel.paths import normalized_home

    parent = tmp_path / "mes-donnees"
    contenu = parent / "content-agents"
    contenu.mkdir(parents=True)

    assert normalized_home(contenu) == parent.resolve()
    assert normalized_home(parent) == parent.resolve()


def test_a_renamed_content_directory_is_recognised_by_its_shape(tmp_path) -> None:
    """Un dossier renommé reste reconnaissable à ce qu'il contient."""
    from agentic_kernel.paths import normalized_home

    parent = tmp_path / "ailleurs"
    contenu = parent / "amk-data"
    (contenu / "agents").mkdir(parents=True)
    (contenu / "system.md").write_text("# instructions", encoding="utf-8")

    assert normalized_home(contenu) == parent.resolve()


def test_an_ordinary_directory_is_left_alone(tmp_path) -> None:
    """Sans marqueur, on prend le chemin tel quel : c'est un parent."""
    from agentic_kernel.paths import normalized_home

    ordinaire = tmp_path / "documents"
    ordinaire.mkdir()

    assert normalized_home(ordinaire) == ordinaire.resolve()
