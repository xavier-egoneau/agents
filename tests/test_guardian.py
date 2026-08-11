import os
from pathlib import Path

import pytest

from agentic_kernel.guardian import review_tool_call
from agentic_kernel.models import GuardianVerdict, SecurityMode, ToolRisk


def decide(tmp_path: Path, tool: str, risks: list[ToolRisk], mode: SecurityMode, path: str):
    return review_tool_call(
        tool_name=tool,
        tool_call_id="call-1",
        agent_id="main",
        arguments={"path": path, "justification": "Required by the user."},
        risks=risks,
        mode=mode,
        workspace=tmp_path,
    )


def test_filesystem_permission_matrix(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("old")
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.SAFE, "existing.txt").verdict
        == GuardianVerdict.ALLOW
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.SAFE, "new.txt").verdict
        == GuardianVerdict.ASK
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.LIMITED, "new.txt").verdict
        == GuardianVerdict.ALLOW
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.LIMITED, "existing.txt").verdict
        == GuardianVerdict.ASK
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.POWER, "existing.txt").verdict
        == GuardianVerdict.ALLOW
    )
    for mode in SecurityMode:
        assert (
            decide(tmp_path, "delete", [ToolRisk.DESTRUCTIVE], mode, "existing.txt").verdict
            == GuardianVerdict.ASK
        )


def test_outside_and_protected_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, str(outside)).verdict
        == GuardianVerdict.ASK
    )
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, ".ssh/id_rsa").verdict
        == GuardianVerdict.DENY
    )
    outside.write_text("secret")
    try:
        (tmp_path / "link").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable for this user: {exc}")
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, "link").verdict
        == GuardianVerdict.DENY
    )


def test_kernel_owned_session_artifact_is_readable_without_ask(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    artifact_root = tmp_path / "content-agents" / "sessions" / "artifacts" / "session-1"
    artifact = artifact_root / "run-1" / "screen.png"
    workspace.mkdir()
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"png")

    decision = review_tool_call(
        tool_name="image_inspect",
        tool_call_id="call-image",
        agent_id="main",
        arguments={"path": str(artifact), "justification": "Inspect generated screenshot."},
        risks=[ToolRisk.READ],
        mode=SecurityMode.POWER,
        workspace=workspace,
        trusted_read_roots=(artifact_root,),
    )

    assert decision.verdict == GuardianVerdict.ALLOW


def test_missing_justification_is_denied(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="read",
        tool_call_id="call-1",
        agent_id="main",
        arguments={"path": "file.txt"},
        risks=[ToolRisk.READ],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
    )
    assert decision.verdict == GuardianVerdict.DENY


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
@pytest.mark.parametrize(
    "path",
    ["note.txt:secret", r"\\?\C:\Windows\system.ini", "NUL", "COM1.txt"],
)
def test_windows_ambiguous_and_device_paths_are_denied(tmp_path: Path, path: str) -> None:
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, path).verdict
        == GuardianVerdict.DENY
    )


def test_loopback_is_allowed_in_power_mode(tmp_path: Path) -> None:
    """`localhost` désigne cette machine, celle que `power` autorise déjà.

    L'agent y exécute des commandes et y lit des fichiers : lui redemander
    l'ouverture du serveur qu'il vient de lancer n'ajoute aucune protection,
    seulement une confirmation de plus — et trois d'affilée pour une seule
    vérification de rendu.
    """
    decision = review_tool_call(
        tool_name="browser_open",
        tool_call_id="call-open",
        agent_id="main",
        arguments={
            "url": "http://localhost:8123",
            "justification": "Vérifier le rendu servi en local.",
        },
        risks=[ToolRisk.NETWORK, ToolRisk.EXTERNAL],
        mode=SecurityMode.POWER,
        workspace=tmp_path,
    )

    assert decision.verdict == GuardianVerdict.ALLOW


def test_loopback_still_asks_outside_power(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="browser_open",
        tool_call_id="call-open",
        agent_id="main",
        arguments={"url": "http://localhost:8123", "justification": "Vérifier."},
        risks=[ToolRisk.NETWORK, ToolRisk.EXTERNAL],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
    )

    assert decision.verdict == GuardianVerdict.ASK


def test_another_machine_on_the_lan_always_asks(tmp_path: Path) -> None:
    """Une adresse privée non locale désigne le routeur, un NAS, un service
    interne — pas cette machine. La confirmation garde son sens, même en
    `power`."""
    decision = review_tool_call(
        tool_name="web",
        tool_call_id="call-web",
        agent_id="main",
        arguments={
            "action": "scrape",
            "url": "http://192.168.1.1/admin",
            "justification": "Lire la page.",
        },
        risks=[ToolRisk.NETWORK, ToolRisk.EXTERNAL],
        mode=SecurityMode.POWER,
        workspace=tmp_path,
    )

    assert decision.verdict == GuardianVerdict.ASK


def test_private_web_target_always_requires_approval(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="web",
        tool_call_id="call-web",
        agent_id="main",
        arguments={
            "action": "scrape",
            "url": "http://127.0.0.1:8080/private",
            "justification": "Inspect a local service.",
        },
        risks=[ToolRisk.NETWORK, ToolRisk.EXTERNAL],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
    )
    assert decision.verdict == GuardianVerdict.ASK
    assert decision.path == "http://127.0.0.1:8080"


def test_declared_path_parameter_is_checked_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    decision = review_tool_call(
        tool_name="knowledge_ingest",
        tool_call_id="call-ingest",
        agent_id="main",
        arguments={"source": str(outside), "justification": "Archive this document."},
        risks=[ToolRisk.READ, ToolRisk.WRITE],
        mode=SecurityMode.POWER,
        workspace=tmp_path,
        path_parameters=("source",),
        url_parameters=("source",),
    )
    assert decision.verdict == GuardianVerdict.ASK
    assert decision.path == str(outside.resolve())


def test_declared_url_parameter_catches_private_target(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="knowledge_ingest",
        tool_call_id="call-ingest",
        agent_id="main",
        arguments={
            "source": "http://127.0.0.1:9000/private",
            "justification": "Archive this page.",
        },
        risks=[ToolRisk.READ, ToolRisk.NETWORK, ToolRisk.WRITE],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
        path_parameters=("source",),
        url_parameters=("source",),
    )
    assert decision.verdict == GuardianVerdict.ASK
    assert decision.path == "http://127.0.0.1:9000"


def test_the_agent_personal_workspace_is_not_gated_on_overwrite(tmp_path: Path) -> None:
    """Le rangement de l'agent n'est pas le travail de l'utilisateur.

    Demander à chaque écriture de `USER.md` entraîne à valider sans lire — et le
    jour où la demande porte sur un fichier qui compte, elle passe avec les
    autres.
    """
    personnel = tmp_path / "workspaces" / "main"
    personnel.mkdir(parents=True)
    memoire = personnel / "USER.md"
    memoire.write_text("# User profile\n", encoding="utf-8")

    decision = review_tool_call(
        tool_name="write",
        tool_call_id="1",
        agent_id="main",
        arguments={"path": str(memoire), "content": "x", "justification": "mémoriser"},
        risks=[ToolRisk.WRITE],
        mode=SecurityMode.LIMITED,
        workspace=personnel,
        personal_workspace=personnel,
    )

    assert decision.verdict is GuardianVerdict.ALLOW


def test_overwriting_a_user_file_still_asks_in_limited_mode(tmp_path: Path) -> None:
    """L'exemption vise le dossier de l'agent, pas le répertoire de travail :
    l'élargir ferait disparaître la garde devant les fichiers du projet."""
    projet = tmp_path / "projet"
    projet.mkdir()
    source = projet / "app.ts"
    source.write_text("export const a = 1;\n", encoding="utf-8")

    decision = review_tool_call(
        tool_name="write",
        tool_call_id="1",
        agent_id="main",
        arguments={"path": str(source), "content": "x", "justification": "corriger"},
        risks=[ToolRisk.WRITE],
        mode=SecurityMode.LIMITED,
        workspace=projet,
        personal_workspace=tmp_path / "workspaces" / "main",
    )

    assert decision.verdict is GuardianVerdict.ASK
    assert "Overwriting" in decision.reason


def test_deleting_inside_the_personal_workspace_still_asks(tmp_path: Path) -> None:
    """Effacer une page d'encyclopédie n'a pas de filet : le tag destructif
    reste traité avant l'exemption."""
    personnel = tmp_path / "workspaces" / "main"
    personnel.mkdir(parents=True)
    page = personnel / "knowledge" / "sujet.md"
    page.parent.mkdir()
    page.write_text("contenu", encoding="utf-8")

    decision = review_tool_call(
        tool_name="move",
        tool_call_id="1",
        agent_id="main",
        arguments={"path": str(page), "justification": "ranger"},
        risks=[ToolRisk.WRITE, ToolRisk.DESTRUCTIVE],
        mode=SecurityMode.LIMITED,
        workspace=personnel,
        personal_workspace=personnel,
    )

    assert decision.verdict is GuardianVerdict.ASK


def _grants(*couples: tuple[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset(couples)


def test_a_tool_declared_by_the_accepted_workflow_runs_without_asking(tmp_path: Path) -> None:
    """Le contrat a été lu et validé avant d'être enregistré.

    Redemander appel par appel fait valider deux fois la même chose — et une
    routine s'exécute sans personne devant l'écran : une demande à six heures du
    matin ne protège rien, elle interrompt.
    """
    decision = review_tool_call(
        tool_name="icloud_list_events",
        tool_call_id="1",
        agent_id="main",
        arguments={"days_ahead": 7, "justification": "point du jour"},
        risks=[ToolRisk.READ, ToolRisk.EXTERNAL],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
        workflow_grants=_grants(("icloud_list_events", "")),
    )

    assert decision.verdict is GuardianVerdict.ALLOW
    assert "workflow accepté" in decision.reason


def test_a_tool_absent_from_the_workflow_still_asks(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="http_request",
        tool_call_id="1",
        agent_id="main",
        arguments={"url": "https://exemple.test", "method": "POST", "justification": "envoi"},
        risks=[ToolRisk.NETWORK],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
        workflow_grants=_grants(("icloud_list_events", "")),
    )

    assert decision.verdict is GuardianVerdict.ASK


def test_fixed_arguments_narrow_what_the_contract_grants(tmp_path: Path) -> None:
    """Une déclaration qui fige ses arguments n'autorise pas au-delà."""
    figes = '{"calendar":"Famille"}'

    conforme = review_tool_call(
        tool_name="icloud_list_events",
        tool_call_id="1",
        agent_id="main",
        arguments={"calendar": "Famille", "justification": "point"},
        risks=[ToolRisk.EXTERNAL],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
        workflow_grants=_grants(("icloud_list_events", figes)),
    )
    devie = review_tool_call(
        tool_name="icloud_list_events",
        tool_call_id="2",
        agent_id="main",
        arguments={"calendar": "Travail", "justification": "point"},
        risks=[ToolRisk.EXTERNAL],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
        workflow_grants=_grants(("icloud_list_events", figes)),
    )

    assert conforme.verdict is GuardianVerdict.ALLOW
    assert devie.verdict is GuardianVerdict.ASK


def test_the_contract_never_lifts_a_refusal(tmp_path: Path) -> None:
    """La concession relève ce qui aurait été demandé, jamais ce qui est interdit.

    Sans cette borne, un workflow accepté deviendrait un chèque en blanc : il
    suffirait de faire déclarer un outil pour contourner les refus du Guardian.
    """
    decision = review_tool_call(
        tool_name="read",
        tool_call_id="1",
        agent_id="main",
        arguments={"path": str(Path.home() / ".ssh" / "id_rsa"), "justification": "lire"},
        risks=[ToolRisk.READ, ToolRisk.SECRET],
        mode=SecurityMode.POWER,
        workspace=tmp_path,
        workflow_grants=_grants(("read", "")),
    )

    assert decision.verdict is GuardianVerdict.DENY


def test_a_command_without_arguments_is_reviewed_rather_than_crashing(tmp_path: Path) -> None:
    """`"args": null` est la façon normale d'un modèle de dire « sans argument ».

    `arguments.get("args", [])` ne protège que de la clé absente : la clé étant
    présente avec `None`, la boucle recevait `None` et le run entier tombait sur
    un `TypeError` avant même que le Guardian ait rendu son avis. Un `dir` sans
    argument suffisait à condamner la conversation.
    """
    decision = review_tool_call(
        tool_name="command_run",
        tool_call_id="call-1",
        agent_id="main",
        arguments={
            "program": "dir",
            "args": None,
            "cwd": ".",
            "network": False,
            "justification": "Lister le dossier.",
        },
        risks=[ToolRisk.EXECUTE],
        mode=SecurityMode.POWER,
        workspace=tmp_path,
    )

    assert decision.verdict is not GuardianVerdict.DENY


def test_an_orchestrator_cannot_write_where_its_children_work(tmp_path: Path) -> None:
    """Une consigne ne contraint pas; le périmètre d'écriture, si.

    Avec « Écrire un fichier de code toi-même est une erreur » en tête de son
    prompt, l'orchestrateur observé a produit vingt-sept `patch` et trois
    `write` dans un projet, sans jamais appeler `agent_delegate`. Le refus
    porte le remède : à qui confier, et avec quoi.
    """
    donnees = tmp_path / "content-agents"
    projet = tmp_path / "projets" / "test8"
    projet.mkdir(parents=True)
    (projet / "index.html").write_text("<html></html>", encoding="utf-8")

    decision = review_tool_call(
        tool_name="patch",
        tool_call_id="call-1",
        agent_id="main",
        arguments={"path": str(projet / "index.html"), "justification": "Corriger le skip-link."},
        risks=[ToolRisk.WRITE],
        mode=SecurityMode.POWER,
        workspace=projet,
        delegates=("dev", "reviewer"),
        delegated_write_exemptions=(donnees,),
    )

    assert decision.verdict is GuardianVerdict.DENY
    assert "dev, reviewer" in decision.reason
    assert "agent_delegate" in decision.reason


def test_an_orchestrator_still_keeps_its_own_files(tmp_path: Path) -> None:
    """Mémoire, bibliothèque, routines et notes ne se délèguent pas."""
    donnees = tmp_path / "content-agents"
    personnel = donnees / "workspaces" / "main"
    personnel.mkdir(parents=True)

    decision = review_tool_call(
        tool_name="write",
        tool_call_id="call-2",
        agent_id="main",
        arguments={
            "path": str(personnel / "knowledge" / "incoming" / "note.md"),
            "justification": "Archiver une source.",
        },
        risks=[ToolRisk.WRITE],
        mode=SecurityMode.POWER,
        workspace=personnel,
        delegates=("dev", "reviewer"),
        delegated_write_exemptions=(donnees,),
    )

    assert decision.verdict is not GuardianVerdict.DENY


def test_an_agent_without_children_writes_as_before(tmp_path: Path) -> None:
    """Refuser l'écriture à qui n'a personne à qui la confier ne mènerait nulle part."""
    projet = tmp_path / "projets" / "test8"
    projet.mkdir(parents=True)

    decision = review_tool_call(
        tool_name="write",
        tool_call_id="call-3",
        agent_id="dev",
        arguments={"path": str(projet / "app.js"), "justification": "Implémenter la tâche."},
        risks=[ToolRisk.WRITE],
        mode=SecurityMode.POWER,
        workspace=projet,
        delegates=(),
        delegated_write_exemptions=(tmp_path / "content-agents",),
    )

    assert decision.verdict is not GuardianVerdict.DENY
