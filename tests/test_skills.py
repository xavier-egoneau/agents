import pytest
from pydantic_ai import ModelRetry

from agentic_kernel.models import SkillConfig
from agentic_kernel.skills import render_skill, skill_catalog_instruction, skill_toolset


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


def test_skill_toolset_loads_full_instructions() -> None:
    toolset = skill_toolset(skills())
    assert toolset is not None
    result = render_skill(skills(), "review")
    assert "Find correctness issues" in result
    assert "Skill root: /tmp/review" in result


def test_skill_toolset_rejects_unknown_skill() -> None:
    toolset = skill_toolset(skills())
    assert toolset is not None
    with pytest.raises(ModelRetry, match="Unknown skill"):
        render_skill(skills(), "missing")
