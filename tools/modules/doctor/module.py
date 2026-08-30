from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.doctor import Diagnostic, diagnose
from agentic_kernel.paths import runtime_layout


def _document(check: Diagnostic) -> dict[str, Any]:
    return asdict(check)


async def doctor(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """Inspect the active AMK installation without changing it."""
    layout = runtime_layout(ctx.deps.workspace)
    checks = diagnose(layout)
    failures = [
        check
        for check in checks
        if check.required and check.status in {"missing", "error"}
    ]
    optional = [check for check in checks if check.status == "optional"]
    warnings = [
        check
        for check in checks
        if check not in failures
        and check not in optional
        and check.status != "ok"
    ]
    status = "unhealthy" if failures else "degraded" if warnings else "healthy"
    return {
        "ok": True,
        "data": {
            "status": status,
            "healthy": not failures and not warnings,
            "failures": [_document(check) for check in failures],
            "warnings": [_document(check) for check in warnings],
            "optional": [_document(check) for check in optional],
            "checks": [_document(check) for check in checks],
        },
        "error": None,
        "metadata": {
            "application_root": str(layout.application_root),
            "content_root": str(layout.content_root),
            "workspace": str(layout.workspace),
        },
    }


class DoctorModule:
    def toolsets(self):
        return [FunctionToolset(tools=[doctor])]

    def instructions(self):
        return [
            "Use `doctor` when an installation, tool catalog, sandbox, provider "
            "or local capability appears unavailable or inconsistent. Do not run "
            "it routinely when there is no platform symptom.",
            "If `doctor` returns `unhealthy` or `degraded`, tell the user explicitly. "
            "Report required failures first, then actionable warnings. A component "
            "listed under `optional` is not a failure unless the requested task needs it.",
        ]

    def capabilities(self):
        return []


module = DoctorModule()
