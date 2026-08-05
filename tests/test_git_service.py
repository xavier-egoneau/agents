from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.git_service import GitError, GitService
from agentic_kernel.kernel import Kernel
from agentic_kernel.models import RunRequest


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("avant\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-m", "Initial")
    return tmp_path


def test_snapshot_uses_repository_containing_workspace(repository: Path) -> None:
    nested = repository / "packages" / "app"
    nested.mkdir(parents=True)
    (repository / "tracked.txt").write_text("avant\naprès\n", encoding="utf-8")
    (nested / "new file.txt").write_text("nouveau\n", encoding="utf-8")

    snapshot = GitService().snapshot(nested)

    assert snapshot.repo_root == str(repository.resolve())
    assert {item.path for item in snapshot.files} == {
        "packages/app/new file.txt",
        "tracked.txt",
    }
    assert snapshot.additions == 2


def test_commit_is_global_and_rejects_a_stale_proposal(repository: Path) -> None:
    service = GitService()
    (repository / "tracked.txt").write_text("modifié\n", encoding="utf-8")
    (repository / "other.txt").write_text("autre\n", encoding="utf-8")
    proposed = service.snapshot(repository)
    (repository / "late.txt").write_text("tardif\n", encoding="utf-8")

    with pytest.raises(GitError, match="a changé"):
        service.commit(repository, "Met à jour le projet", proposed.fingerprint)

    current = service.snapshot(repository)
    service.commit(repository, "Met à jour tout le projet", current.fingerprint)
    assert service.snapshot(repository).files == []
    changed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert {line for line in changed.splitlines() if line} == {
        "late.txt",
        "other.txt",
        "tracked.txt",
    }


def test_switch_refuses_dirty_repository(repository: Path) -> None:
    git(repository, "branch", "other")
    (repository / "tracked.txt").write_text("sale\n", encoding="utf-8")

    with pytest.raises(GitError, match="modifications"):
        GitService().switch(repository, "other")


def test_response_snapshot_only_contains_files_changed_during_run(project: Path) -> None:
    repository = project / "customer-project"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.email", "test@example.com")
    git(repository, "config", "user.name", "Test")
    (repository / "tracked.txt").write_text("avant\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "-m", "Initial")
    preexisting = repository / "preexisting.txt"
    preexisting.write_text("déjà là\n", encoding="utf-8")
    kernel = Kernel(project)
    session_id, run_id = uuid4(), uuid4()
    request = RunRequest(
        session_id=session_id,
        prompt="Modifie le projet",
        workspace=repository,
    )
    baseline = kernel._capture_git_baseline(request, run_id, repository)
    (repository / "tracked.txt").write_text("changé pendant le run\n", encoding="utf-8")

    kernel._capture_git_snapshot(request, run_id, repository, baseline)

    snapshot = next(
        event for event in kernel.events.read(session_id) if event.type == "git.snapshot"
    )
    assert [item["path"] for item in snapshot.payload["files"]] == ["tracked.txt"]
    assert preexisting.exists()  # présent dans le dépôt, absent de la carte


def test_no_snapshot_without_a_baseline_to_compare_with(project: Path) -> None:
    """Sans point de comparaison, tout fichier déjà sale passerait pour une
    modification de l'agent : la carte annoncerait le dépôt entier comme son
    travail. Mieux vaut ne rien afficher."""
    repository = project / "customer-project"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.email", "test@example.com")
    git(repository, "config", "user.name", "Test")
    (repository / "tracked.txt").write_text("avant\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "-m", "Initial")
    (repository / "sale-avant-le-run.txt").write_text("modifié hors AMK\n", encoding="utf-8")
    kernel = Kernel(project)
    session_id, run_id = uuid4(), uuid4()
    request = RunRequest(
        session_id=session_id,
        prompt="Ne touche à rien",
        workspace=repository,
    )

    # Aucun `git.baseline` n'a été émis pour ce run : reprise après approbation,
    # lecture du dépôt en échec, ou événement absent de l'historique.
    kernel._capture_git_snapshot(request, run_id, repository)

    assert not [
        event for event in kernel.events.read(session_id) if event.type == "git.snapshot"
    ]
