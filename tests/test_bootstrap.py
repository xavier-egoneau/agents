"""Tests du socle d'installation.

`content-agents/` n'étant pas versionné, installer AMK sur une seconde machine
supposait de copier ce dossier — ce qui transportait aussi l'état local :
routines pointant vers des dossiers inexistants, mémoires d'une autre machine,
clés API. Ces tests fixent le comportement du socle qui remplace cette copie.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agentic_kernel.bootstrap import (
    DEFAULTS_ROOT,
    ensure_content_root,
    missing_configuration,
)

# Valeurs réelles relevées dans l'espace utilisateur au moment de l'extraction.
# Elles n'ont rien à faire dans un paquet distribué.
SECRET_MARKERS = ("zmhg-ckmc", "xavier.egoneau", "xavieregoneau", "sk-")


def test_cold_install_provides_the_reference_content(tmp_path: Path) -> None:
    content_root = tmp_path / "content-agents"

    report = ensure_content_root(content_root)

    assert report.initialized
    assert (content_root / "system.md").is_file()
    assert (content_root / "agents" / "main.md").is_file()
    assert (content_root / "workspaces" / "main").is_dir()
    installed = {path.name for path in (content_root / "skills").iterdir()}
    assert {"dev", "explore", "model-context", "plan-build", "skill-creator"} <= installed
    assert (content_root / "skills" / "workflow-creator" / "references").is_dir()


def test_second_run_changes_nothing(tmp_path: Path) -> None:
    """La fonction tourne à chaque démarrage : elle doit être idempotente."""
    content_root = tmp_path / "content-agents"
    ensure_content_root(content_root)

    report = ensure_content_root(content_root)

    assert report.created == []
    assert not report.initialized
    assert report.kept


def test_user_edits_are_never_overwritten(tmp_path: Path) -> None:
    content_root = tmp_path / "content-agents"
    ensure_content_root(content_root)
    skill = content_root / "skills" / "dev" / "SKILL.md"
    skill.write_text("# Ma version\n", encoding="utf-8")

    ensure_content_root(content_root)

    assert skill.read_text(encoding="utf-8") == "# Ma version\n"


def test_unchanged_managed_file_receives_a_new_default(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults"
    target = tmp_path / "content-agents"
    defaults.mkdir()
    source = defaults / "system.md"
    source.write_text("version 1\n", encoding="utf-8")
    ensure_content_root(target, defaults=defaults)

    source.write_text("version 2\n", encoding="utf-8")
    report = ensure_content_root(target, defaults=defaults)

    assert (target / "system.md").read_text(encoding="utf-8") == "version 2\n"
    assert report.updated == ["system.md"]


def test_modified_file_does_not_receive_a_new_default(tmp_path: Path) -> None:
    defaults = tmp_path / "defaults"
    target = tmp_path / "content-agents"
    defaults.mkdir()
    source = defaults / "system.md"
    source.write_text("version 1\n", encoding="utf-8")
    ensure_content_root(target, defaults=defaults)
    (target / "system.md").write_text("ma version\n", encoding="utf-8")

    source.write_text("version 2\n", encoding="utf-8")
    report = ensure_content_root(target, defaults=defaults)

    assert (target / "system.md").read_text(encoding="utf-8") == "ma version\n"
    assert report.updated == []


def test_a_deleted_skill_is_restored(tmp_path: Path) -> None:
    content_root = tmp_path / "content-agents"
    ensure_content_root(content_root)
    shutil.rmtree(content_root / "skills" / "explore")

    report = ensure_content_root(content_root)

    assert (content_root / "skills" / "explore" / "SKILL.md").is_file()
    assert "skills/explore/SKILL.md" in report.created


def test_configuration_is_offered_as_templates_only(tmp_path: Path) -> None:
    """Un `providers.json` créé d'office masquerait l'absence de clé."""
    content_root = tmp_path / "content-agents"

    ensure_content_root(content_root)

    assert (content_root / "providers.example.json").is_file()
    assert not (content_root / "providers.json").exists()
    assert not (content_root / "secrets.json").exists()


def test_only_the_blocking_configuration_is_reported_by_default(tmp_path: Path) -> None:
    """Signaler chaque gabarit à chaque démarrage produirait un bruit permanent."""
    content_root = tmp_path / "content-agents"
    ensure_content_root(content_root)

    assert missing_configuration(content_root) == ["providers.json"]
    assert missing_configuration(content_root, include_optional=True) == [
        "providers.json",
        "rag.json",
        "secrets.json",
    ]

    (content_root / "providers.json").write_text("{}", encoding="utf-8")
    assert missing_configuration(content_root) == []


def test_the_shipped_defaults_carry_no_secret() -> None:
    """Le socle est distribué : il ne doit contenir ni clé, ni identité, ni chemin."""
    for path in DEFAULTS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for marker in SECRET_MARKERS:
            assert marker not in content, f"{path.name} contient {marker!r}"


def test_shipped_whitelist_is_empty() -> None:
    """Une liste de chemins autorisés n'a de sens que sur la machine qui l'a écrite."""
    import json

    document = json.loads((DEFAULTS_ROOT / "whitelist_paths.json").read_text(encoding="utf-8"))

    assert document["paths"] == []


def test_local_state_is_never_shipped() -> None:
    names = {path.name for path in DEFAULTS_ROOT.rglob("*") if path.is_file()}

    assert "state.db" not in names
    assert "secrets.json" not in names
    assert "providers.json" not in names
    assert "crons.json" not in names
