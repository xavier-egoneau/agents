from pathlib import Path

import pytest

from agentic_kernel.config import ProjectConfig
from agentic_kernel.errors import ConfigurationError
from agentic_kernel.models import ConnectionType


def test_loads_markdown_agent_and_infers_legacy_provider(project: Path) -> None:
    config = ProjectConfig(project)
    assert config.agents()["main"].instructions == "Answer the request."
    assert config.providers().providers[0].connection_type == ConnectionType.LOCAL


def test_user_memory_is_opt_in_and_loaded_from_agent_workspace(project: Path) -> None:
    agent_path = project / "content-agents" / "agents" / "main.md"
    content = agent_path.read_text(encoding="utf-8").replace(
        "provider: test\n",
        "provider: test\nuser_memory: true\n",
    )
    agent_path.write_text(content, encoding="utf-8")
    config = ProjectConfig(project)

    assert config.agents()["main"].user_memory is True
    config.user_memory_instruction("main")
    workspace = project / "content-agents" / "workspaces" / "main"
    user_path = workspace / "USER.md"
    decisions_path = workspace / "DECISIONS.md"
    assert user_path.is_file()
    assert decisions_path.is_file()

    user_path.write_text("# User profile\n\n- Name: Camille\n", encoding="utf-8")
    decisions_path.write_text(
        "# Decisions\n\n- 2026-08-05: Prefer concise answers.\n",
        encoding="utf-8",
    )
    instruction = config.user_memory_instruction("main")
    assert "Camille" in instruction
    assert "Prefer concise answers" in instruction
    assert str(user_path) in instruction


def test_a_cycle_is_broken_by_the_two_level_rule(project: Path) -> None:
    """La hiérarchie à deux niveaux rend le cycle impossible par construction.

    Chaque délégation vers un orchestrateur est écartée avant même que la
    détection de cycle ne s'exécute ; celle-ci reste en place comme filet, mais
    elle n'est plus la première ligne de défense.
    """
    agents = project / "content-agents" / "agents"
    (agents / "main.md").write_text(
        """---
id: main
description: Main
provider: test
delegates: [child]
---
Main.
""",
        encoding="utf-8",
    )
    (agents / "child.md").write_text(
        """---
id: child
description: Child
provider: test
delegates: [main]
---
Child.
""",
        encoding="utf-8",
    )
    config = ProjectConfig(project)
    agents = config.agents()

    assert agents["main"].delegates == []
    assert agents["child"].delegates == []
    assert len(config.agent_warnings) == 2


def test_loads_claude_markdown_and_codex_toml_agents(project: Path) -> None:
    claude_agents = project / ".claude" / "agents"
    codex_agents = project / ".codex" / "agents"
    claude_agents.mkdir(parents=True)
    codex_agents.mkdir(parents=True)
    (claude_agents / "reviewer.md").write_text(
        """---
name: reviewer
description: Reviews changes
model: sonnet
tools: Read, Grep
skills: [review]
---
Review carefully.
""",
        encoding="utf-8",
    )
    (codex_agents / "writer.toml").write_text(
        """name = "writer"
description = "Writes changes"
model = "gpt-5.2-codex"
developer_instructions = "Write precise changes."
""",
        encoding="utf-8",
    )
    agents = ProjectConfig(project).agents()
    assert agents["reviewer"].provider == "claude"
    assert agents["reviewer"].model == "claude-sonnet-4-6"
    assert agents["reviewer"].skills == ["review"]
    assert "Tools requested" in agents["reviewer"].instructions
    assert agents["writer"].provider == "openai-codex"


def test_discovers_shared_skill_format(project: Path) -> None:
    directory = project / "content-agents" / "skills" / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        """---
name: review
description: Review a change carefully.
allowed-tools: Read, Grep
---
# Review

Find correctness problems and explain them.
""",
        encoding="utf-8",
    )
    skill = ProjectConfig(project).skills()["review"]
    assert skill.allowed_tools == ["Read", "Grep"]
    assert skill.root == str(directory)
    assert "Find correctness problems" in skill.instructions


def test_does_not_discover_skills_outside_content_agents(project: Path) -> None:
    directory = project / ".codex" / "skills" / "unrelated"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: unrelated\ndescription: Must stay outside AMK.\n---\nIgnored.",
        encoding="utf-8",
    )
    assert "unrelated" not in ProjectConfig(project).skills()


def test_native_reprise_command_is_always_available(project: Path) -> None:
    config = ProjectConfig(project)
    command = config.resolve_command("/reprise vérifie d'abord le serveur", project)
    assert command is not None
    assert command["kind"] == "native"
    expanded = config.expand_native_command("/reprise vérifie d'abord le serveur", "/reprise")
    assert "Ne rejoue pas" in expanded
    assert "vérifie d'abord le serveur" in expanded
    commands = {item["command"] for item in config.commands(project)}
    assert {
        "/compact",
        "/context",
        "/model-context",
        "/reprise",
        "/secret",
        "/secret_list",
    } <= commands


def _agent(tmp_path, agent_id, *, subagent=None, delegates=(), provider="test"):
    frontmatter = [
        "---",
        f"id: {agent_id}",
        f"description: {agent_id}",
        f"provider: {provider}",
        "modules: []",
    ]
    if subagent is not None:
        frontmatter.append(f"subagent: {str(subagent).lower()}")
    if delegates:
        frontmatter.append("delegates:")
        frontmatter += [f"  - {name}" for name in delegates]
    frontmatter += ["---", f"Instructions de {agent_id}.", ""]
    path = tmp_path / "content-agents" / "agents" / f"{agent_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(frontmatter), encoding="utf-8")


def test_sub_agents_are_shared_by_every_orchestrator(project: Path) -> None:
    """Créer un orchestrateur ne doit pas obliger à réénumérer les sous-agents.

    Ils forment une bibliothèque partagée : en ajouter un doit profiter à tous
    sans éditer chaque orchestrateur.
    """
    _agent(project, "chef", provider="test")
    _agent(project, "second", provider="test")
    _agent(project, "ouvrier", subagent=True, provider="test")

    agents = ProjectConfig(project).agents()

    assert agents["chef"].delegates == ["ouvrier"]
    assert agents["second"].delegates == ["ouvrier"]
    # Un sous-agent ne délègue à personne, même sans liste explicite.
    assert agents["ouvrier"].delegates == []


def test_an_explicit_list_restricts_instead_of_extending(project: Path) -> None:
    _agent(project, "chef", delegates=["premier"], provider="test")
    _agent(project, "premier", subagent=True, provider="test")
    _agent(project, "second", subagent=True, provider="test")

    assert ProjectConfig(project).agents()["chef"].delegates == ["premier"]


def test_an_orchestrator_cannot_delegate_to_another_orchestrator(project: Path) -> None:
    """Deux niveaux, pas davantage : une chaîne d'orchestrateurs rendrait la
    bibliothèque de connaissance ambiguë, puisqu'elle suit la racine du run."""
    _agent(project, "chef", delegates=["autre"], provider="test")
    _agent(project, "autre", subagent=False, provider="test")

    config = ProjectConfig(project)
    assert config.agents()["chef"].delegates == []
    assert "orchestrateurs" in config.agent_warnings[0]["message"]


def test_an_invalid_hierarchy_is_reported_without_blocking(project: Path) -> None:
    """Une règle d'organisation ne doit pas rendre l'application inutilisable.

    Le refus emportait tout le catalogue, y compris les écrans qui auraient
    permis de corriger la configuration fautive : l'utilisateur se retrouvait
    dehors sans recours. La délégation est retirée, l'agent se charge, et
    l'avertissement remonte jusqu'à l'interface.
    """
    _agent(project, "chef", delegates=["ouvrier"], provider="test")
    _agent(project, "ouvrier", subagent=False, provider="test")
    _agent(project, "isole", provider="test")

    config = ProjectConfig(project)
    agents = config.agents()

    assert {"chef", "ouvrier", "isole"} <= set(agents)
    assert len(config.agent_warnings) == 1
    assert config.agent_warnings[0]["dropped"] == ["ouvrier"]


def test_a_stripped_delegation_falls_back_to_the_shared_sub_agents(project: Path) -> None:
    """Retirer sa seule délégation ne doit pas laisser l'orchestrateur démuni.

    Il retombe sur la bibliothèque partagée, comme s'il n'avait jamais rien
    déclaré — c'est le comportement par défaut, et le plus utile.
    """
    _agent(project, "sophie", delegates=["chef"], provider="test")
    _agent(project, "chef", delegates=["ouvrier"], provider="test")
    _agent(project, "ouvrier", subagent=True, provider="test")

    agents = ProjectConfig(project).agents()

    assert agents["sophie"].delegates == ["ouvrier"]


def test_a_sub_agent_cannot_delegate(project: Path) -> None:
    _agent(project, "chef", provider="test")
    _agent(project, "ouvrier", subagent=True, delegates=["aide"], provider="test")
    _agent(project, "aide", subagent=True, provider="test")

    config = ProjectConfig(project)
    assert config.agents()["ouvrier"].delegates == []
    assert "ne peut pas déléguer" in config.agent_warnings[0]["message"]


def test_an_unknown_delegate_still_fails(project: Path) -> None:
    """Écarter une délégation invalide reste possible ; inventer un agent, non.

    Le graphe est alors réellement cassé, et le silence produirait une
    orchestration qui échoue plus tard, sans explication.
    """
    _agent(project, "chef", delegates=["fantome"], provider="test")

    with pytest.raises(ConfigurationError, match="unknown delegates"):
        ProjectConfig(project).agents()


def test_agents_written_before_the_level_field_still_load(project: Path) -> None:
    """Le champ `subagent` est arrivé après les agents.

    L'exiger rendait invalides des configurations qui fonctionnaient, et le
    socle ne peut pas le rétro-ajouter aux fichiers que l'utilisateur a
    modifiés : l'application ne démarrait plus, sans recours depuis l'interface.
    """
    _agent(project, "chef", delegates=["ouvrier"], provider="test")
    _agent(project, "ouvrier", provider="test")  # aucun marqueur, comme avant
    _agent(project, "autonome", provider="test")

    agents = ProjectConfig(project).agents()

    # Délégué et ne déléguant à personne : c'était déjà la définition d'un
    # sous-agent avant que le champ existe.
    assert agents["ouvrier"].subagent is True
    # Jamais délégué : il reste orchestrateur, et garde donc sa bibliothèque.
    assert agents["autonome"].subagent is False


def test_a_declared_level_always_wins_over_deduction(project: Path) -> None:
    _agent(project, "chef", delegates=["refuse"], provider="test")
    _agent(project, "refuse", subagent=False, provider="test")

    config = ProjectConfig(project)
    # Sans la déclaration, `refuse` aurait été déduit sous-agent et accepté.
    assert config.agents()["chef"].delegates == []
    assert "orchestrateurs" in config.agent_warnings[0]["message"]


def test_deduction_never_flattens_a_chain_of_orchestrators(project: Path) -> None:
    """Un agent qui délègue lui-même n'est pas un exécutant : l'accepter en
    silence rétablirait la profondeur qu'on vient d'interdire."""
    _agent(project, "chef", delegates=["milieu"], provider="test")
    _agent(project, "milieu", delegates=["bas"], provider="test")
    _agent(project, "bas", subagent=True, provider="test")

    config = ProjectConfig(project)
    agents = config.agents()

    assert agents["milieu"].subagent is False
    # `milieu` écarté, la chaîne est rompue : chef retombe sur les sous-agents.
    assert agents["chef"].delegates == ["bas"]
    assert "orchestrateurs" in config.agent_warnings[0]["message"]


def test_an_agent_declares_its_default_permission_level(project: Path) -> None:
    """Le niveau appartient à l'agent, pas à la surface.

    Telegram l'imposait en dur : rien ne le montrait, rien ne permettait de le
    changer, et il divergeait du composer sur une même session.
    """
    chemin = project / "content-agents" / "agents" / "prudent.md"
    chemin.write_text(
        "---\nid: prudent\ndescription: Prudent\nprovider: test\nsecurity_mode: safe\n---\nOK.\n",
        encoding="utf-8",
    )

    agents = ProjectConfig(project).agents()

    assert agents["prudent"].security_mode.value == "safe"
    # Non déclaré, il reste au niveau intermédiaire plutôt qu'au plus permissif.
    assert agents["main"].security_mode.value == "limited"


def test_a_broken_front_matter_names_its_file(project: Path) -> None:
    """Sans le chemin, il fallait ouvrir les fichiers un par un.

    Le message disait « agent definition must start with YAML front matter »
    sans dire lequel, alors qu'un dossier en compte une dizaine.
    """
    fautif = project / "content-agents" / "agents" / "casse.md"
    fautif.write_text("id: casse\ndescription: sans delimiteur\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="casse.md"):
        ProjectConfig(project).agents()


def test_a_missing_system_prompt_points_at_the_likely_cause(project: Path) -> None:
    """L'emplacement des données est configurable : une erreur « fichier
    introuvable » vient le plus souvent d'un chemin mal réglé, pas d'un fichier
    effacé. Le dire épargne la mauvaise piste."""
    (project / "content-agents" / "system.md").unlink()

    with pytest.raises(ConfigurationError, match="dossier de données"):
        ProjectConfig(project).system_instructions()


def test_the_skill_index_survives_a_relocated_data_folder(project: Path) -> None:
    """L'index se calculait relativement à la racine applicative.

    Depuis que le dossier de données est déplaçable, une skill vit ailleurs et
    `relative_to` lève une `ValueError` — qui n'est pas une `ConfigurationError`,
    échappe donc au filtre du routeur et ressort en « Internal Server Error » au
    moment d'enregistrer une skill.
    """
    directory = project / "content-agents" / "skills" / "veille"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: veille\ndescription: Faire la veille.\n---\nChercher.\n",
        encoding="utf-8",
    )
    config = ProjectConfig(project)
    # La skill vit hors de la racine applicative, comme après un déplacement.
    config.root = project / "ailleurs"

    index = config.build_skills_index()

    assert index["skills"][0]["source"].endswith("SKILL.md")
