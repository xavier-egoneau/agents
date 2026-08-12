from __future__ import annotations

from collections.abc import Collection

from pydantic_ai import FunctionToolset, ModelRetry

from .models import SkillConfig, SkillLoad


def _string_metadata(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _admission_metadata(
    skill: SkillConfig,
) -> tuple[list[str], list[str], list[str], list[str], str]:
    """Return optional, compact routing hints without loading the skill body."""
    extra = skill.model_extra or {}
    amk = extra.get("amk") if isinstance(extra.get("amk"), dict) else {}
    scopes = _string_metadata(amk.get("scopes", amk.get("scope")))
    phases = _string_metadata(amk.get("phases", amk.get("phase")))
    positive = _string_metadata(amk.get("positive_signals"))
    negative = _string_metadata(amk.get("negative_signals"))
    retain_until = str(amk.get("retain_until", "")).strip()
    return scopes, phases, positive, negative, retain_until


def _context_cost(skill: SkillConfig) -> int:
    """Cheap estimate used only to make the admission trade-off visible."""
    return max(1, (len(render_skill({skill.name: skill}, skill.name).encode("utf-8")) + 3) // 4)


def skill_catalog_instruction(
    skills: dict[str, SkillConfig],
    attached: Collection[str] = (),
    already_loaded: Collection[str] = (),
) -> str | None:
    """Index des skills : leur nom, leur description, et rien de plus.

    Le corps d'une skill pèse entre 200 et 1 600 tokens. Les inscrire toutes
    dans le prompt à chaque requête coûtait ici près de 10 000 tokens par tour,
    payés que la skill serve ou non. L'index en coûte 300, et `load_skills`
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
        "Skills are reusable instruction packages. This index carries only compact admission "
        "cards; their full bodies are not active yet.",
        "",
        "Before acting, perform skill admission for the next three likely actions. Select the "
        "smallest set whose instructions would materially change one of those actions. General "
        "usefulness or possible later relevance is not enough. Call `load_skills` once with that "
        "set and a short reason. If none qualifies, proceed without loading one. Reconsider only "
        "when the task phase changes or a missing capability becomes concrete.",
        "",
        "Available skills:",
    ]
    for skill in skills.values():
        marques = []
        if skill.name in attaches:
            marques.append("attached to you")
        if skill.name in chargees:
            marques.append("already loaded below")
        scopes, phases, positive, negative, retain_until = _admission_metadata(skill)
        if scopes:
            marques.append("scope: " + "/".join(scopes))
        if phases:
            marques.append("phases: " + "/".join(phases))
        if positive:
            marques.append("use when: " + "; ".join(positive))
        if negative:
            marques.append("skip when: " + "; ".join(negative))
        if retain_until:
            marques.append("retain until: " + retain_until)
        suffixe = f" [{', '.join(marques)}]" if marques else ""
        lines.append(
            f"- {skill.name}{suffixe}: {skill.description} "
            f"(full body ~{_context_cost(skill)} tokens)"
        )
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


def skill_toolset(
    skills: dict[str, SkillConfig], already_loaded: Collection[str] = ()
) -> FunctionToolset | None:
    if not skills:
        return None

    loaded = set(already_loaded)

    def load_skills(names: list[str], reason: str) -> str:
        """Admit the smallest set of skills that changes the next three actions."""
        uniques = list(dict.fromkeys(names))
        unknown = [name for name in uniques if name not in skills]
        if unknown:
            available = ", ".join(sorted(skills))
            raise ModelRetry(
                f"Unknown skills: {', '.join(repr(name) for name in unknown)}. "
                f"Available skills: {available}."
            )
        nouvelles = [name for name in uniques if name not in loaded]
        if not nouvelles:
            return "No new skill admitted; the requested skills are already active."
        loaded.update(nouvelles)
        justification = reason.strip() or "Required for the next actions."
        corps = "\n\n".join(render_skill(skills, name) for name in nouvelles)
        return (
            "# Skills admitted for the current task phase\n\n"
            f"Reason: {justification}\n"
            f"Active additions: {', '.join(nouvelles)}\n\n{corps}"
        )

    return FunctionToolset(tools=[load_skills])


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
