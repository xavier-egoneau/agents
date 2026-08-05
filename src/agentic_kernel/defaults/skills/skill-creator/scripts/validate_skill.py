#!/usr/bin/env python3
"""Validate one embedded AMK skill and refresh the deterministic skill index."""

from __future__ import annotations

import sys
from pathlib import Path


def fail(message: str) -> int:
    print(f"Skill validation failed: {message}", file=sys.stderr)
    return 1


def main() -> int:
    if len(sys.argv) != 2:
        return fail("usage: validate_skill.py content-agents/skills/<name>")

    project_root = Path(__file__).resolve().parents[4]
    skills_root = (project_root / "content-agents" / "skills").resolve()
    target = Path(sys.argv[1])
    target = (Path.cwd() / target).resolve() if not target.is_absolute() else target.resolve()

    if target.parent != skills_root:
        return fail(f"target must be a direct child of {skills_root}")
    if not (target / "SKILL.md").is_file():
        return fail("SKILL.md is missing")
    if not (target / "agents" / "openai.yaml").is_file():
        return fail("agents/openai.yaml is missing")

    sys.path.insert(0, str(project_root / "src"))
    try:
        from agentic_kernel.config import ProjectConfig

        project = ProjectConfig(project_root)
        skills = project.skills()
        skill = skills.get(target.name)
        if skill is None:
            return fail(f"AMK did not discover {target.name!r}")
        if Path(skill.source).resolve().parent != target:
            return fail("the discovered skill is shadowed by another package")
        project.build_skills_index()
    except Exception as exc:
        return fail(str(exc))

    print(f"Skill {target.name!r} is valid; index.json refreshed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
