from __future__ import annotations

from pydantic_ai import FunctionToolset, ModelRetry

from .models import SkillConfig


def skill_catalog_instruction(skills: dict[str, SkillConfig]) -> str | None:
    if not skills:
        return None
    lines = [
        "Skills are reusable instruction packages. Call `load_skill` before using a relevant "
        "skill that is not already included in your instructions.",
        "",
        "Available skills:",
    ]
    lines.extend(f"- {skill.name}: {skill.description}" for skill in skills.values())
    return "\n".join(lines)


def skill_toolset(skills: dict[str, SkillConfig]) -> FunctionToolset | None:
    if not skills:
        return None

    def load_skill(name: str) -> str:
        """Load a named skill's complete instructions before applying it."""
        return render_skill(skills, name)

    return FunctionToolset(tools=[load_skill])


def render_skill(skills: dict[str, SkillConfig], name: str) -> str:
    skill = skills.get(name)
    if skill is None:
        available = ", ".join(sorted(skills))
        raise ModelRetry(f"Unknown skill {name!r}. Available skills: {available}.")
    support = (
        f"Skill root: {skill.root}. Relative references, scripts, and assets are resolved "
        "from this directory."
    )
    tool_guidance = (
        "\nRequested tools: " + ", ".join(skill.allowed_tools) + "."
        if skill.allowed_tools
        else ""
    )
    return (
        f"# Skill: {skill.name}\n\n{skill.description}\n\n{support}{tool_guidance}"
        f"\n\n{skill.instructions}"
    )
