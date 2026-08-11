from __future__ import annotations

from collections.abc import Collection

from pydantic_ai import FunctionToolset, ModelRetry

from .models import SkillConfig, SkillLoad


def skill_catalog_instruction(
    skills: dict[str, SkillConfig],
    attached: Collection[str] = (),
    already_loaded: Collection[str] = (),
) -> str | None:
    """Index des skills : leur nom, leur description, et rien de plus.

    Le corps d'une skill pèse entre 200 et 1 600 tokens. Les inscrire toutes
    dans le prompt à chaque requête coûtait ici près de 10 000 tokens par tour,
    payés que la skill serve ou non. L'index en coûte 300, et `load_skill`
    apporte le reste au moment où il sert.

    `attached` distingue les skills rattachées à l'agent — son répertoire
    habituel, celles qu'il doit envisager en premier — de celles simplement
    disponibles. `already_loaded` retire de l'invitation à charger celles dont
    le corps est déjà dans les instructions.
    """
    if not skills:
        return None
    attaches = set(attached)
    chargees = set(already_loaded)
    lines = [
        "Skills are reusable instruction packages. This index carries only their names and "
        "descriptions. Call `load_skill` to read a skill's full instructions before applying "
        "it — do this as soon as a skill looks relevant, not after improvising without it.",
        "",
        "Available skills:",
    ]
    for skill in skills.values():
        marques = []
        if skill.name in attaches:
            marques.append("attached to you")
        if skill.name in chargees:
            marques.append("already loaded below")
        suffixe = f" [{', '.join(marques)}]" if marques else ""
        lines.append(f"- {skill.name}{suffixe}: {skill.description}")
    return "\n".join(lines)


def inlined_skills(
    skills: dict[str, SkillConfig],
    requested: Collection[str],
    forced: Collection[str] = (),
) -> list[str]:
    """Parmi les skills demandées, celles dont le corps entre dans le prompt.

    Deux raisons de l'inscrire d'office : la skill se déclare `load: always`,
    ou elle a été demandée explicitement pour ce run — une commande, un
    workflow. Dans ce second cas la décision est déjà prise, faire charger le
    modèle par un aller-retour supplémentaire ne servirait qu'à retarder.
    """
    forcees = set(forced)
    return [
        name
        for name in requested
        if name in forcees or skills[name].load is SkillLoad.ALWAYS
    ]


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
        "\nRequested tools: " + ", ".join(skill.allowed_tools) + "." if skill.allowed_tools else ""
    )
    return (
        f"# Skill: {skill.name}\n\n{skill.description}\n\n{support}{tool_guidance}"
        f"\n\n{skill.instructions}"
    )
