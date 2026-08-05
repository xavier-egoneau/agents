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


def test_rejects_agent_cycle(project: Path) -> None:
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
    with pytest.raises(ConfigurationError, match="cycle"):
        ProjectConfig(project).agents()


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
