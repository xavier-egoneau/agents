import pytest
from pydantic_ai import ModelRetry

from agentic_kernel.models import SkillConfig
from agentic_kernel.skills import skill_catalog_instruction, skill_toolset


def skills():
    return {
        "review": SkillConfig(
            name="review",
            description="Review changes",
            instructions="Find correctness issues.",
            source="/tmp/review/SKILL.md",
            root="/tmp/review",
            allowed_tools=["Read"],
        )
    }


def test_skill_catalog_uses_progressive_disclosure() -> None:
    catalog = skill_catalog_instruction(skills())
    assert catalog is not None
    assert "review: Review changes" in catalog
    assert "Find correctness issues" not in catalog


def test_skill_catalog_requires_minimal_next_action_admission() -> None:
    catalog = skill_catalog_instruction(skills())

    assert catalog is not None
    assert "next three likely actions" in catalog
    assert "smallest set" in catalog
    assert "General usefulness or possible later relevance is not enough" in catalog
    assert "load_skills" in catalog
    assert "~" in catalog and "tokens" in catalog


def test_skill_catalog_exposes_optional_generic_routing_hints() -> None:
    routed = skills()
    routed["review"] = routed["review"].model_copy(
        update={
            "amk": {
                "phases": ["verify"],
                "positive_signals": ["changes need review"],
                "negative_signals": ["no changes exist"],
                "retain_until": "review-completed",
            }
        }
    )

    catalog = skill_catalog_instruction(routed)

    assert catalog is not None
    assert "phases: verify" in catalog
    assert "use when: changes need review" in catalog
    assert "skip when: no changes exist" in catalog
    assert "retain until: review-completed" in catalog


def test_skill_toolset_loads_full_instructions() -> None:
    toolset = skill_toolset(skills())
    assert toolset is not None
    result = toolset.tools["load_skills"].function(
        ["review"], "It changes the next review action."
    )
    assert "Find correctness issues" in result
    assert "Skill root: /tmp/review" in result
    assert "Active additions: review" in result


def test_skill_admission_does_not_reload_an_active_skill() -> None:
    toolset = skill_toolset(skills(), already_loaded=["review"])
    assert toolset is not None

    result = toolset.tools["load_skills"].function(["review"], "Still useful.")

    assert result == "No new skill admitted; the requested skills are already active."


def test_skill_toolset_rejects_unknown_skill() -> None:
    toolset = skill_toolset(skills())
    assert toolset is not None
    with pytest.raises(ModelRetry, match="Unknown skills"):
        toolset.tools["load_skills"].function(["missing"], "Needed.")
