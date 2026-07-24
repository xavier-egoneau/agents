from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.plans import (
    PlanConflict,
    PlanNotFound,
    PlanService,
    PlanStepInput,
    PlanStepStatus,
)
from agentic_kernel.scheduler import CronJobInput, CronService, SchedulerError


def _plans(ctx: RunContext[Any]) -> PlanService:
    path = ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db")
    return PlanService(path)


def _crons(ctx: RunContext[Any]) -> CronService:
    path = ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db")
    return CronService(path)


async def plan_create(
    ctx: RunContext[Any],
    title: str,
    steps: list[PlanStepInput],
    justification: str = "",
) -> dict[str, Any]:
    """Create a bounded structured plan for the current session."""
    plan = _plans(ctx).create(ctx.deps.session_id, title, steps)
    return {"ok": True, "data": plan, "error": None, "metadata": {}}


async def plan_update(
    ctx: RunContext[Any],
    plan_id: str,
    step_id: str,
    status: PlanStepStatus,
    note: str | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Update exactly one step in a session plan."""
    try:
        plan = _plans(ctx).update(
            plan_id,
            step_id,
            status,
            session_id=ctx.deps.session_id,
            note=note,
        )
    except PlanNotFound as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "not_found", "message": str(exc)},
            "metadata": {},
        }
    except PlanConflict as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "dependency_blocked", "message": str(exc)},
            "metadata": {},
        }
    return {"ok": True, "data": plan, "error": None, "metadata": {}}


async def plan_status(
    ctx: RunContext[Any], plan_id: str | None = None, justification: str = ""
) -> dict[str, Any]:
    """Read one current-session plan."""
    service = _plans(ctx)
    try:
        plan = (
            service.get(plan_id, ctx.deps.session_id)
            if plan_id
            else service.current(ctx.deps.session_id)
        )
    except PlanNotFound:
        plan = None
    return (
        {"ok": True, "data": plan, "error": None, "metadata": {}}
        if plan
        else {
            "ok": False,
            "data": None,
            "error": {"type": "not_found", "message": "plan not found"},
            "metadata": {},
        }
    )


async def plan_ready(ctx: RunContext[Any], plan_id: str, justification: str = "") -> dict[str, Any]:
    """Return only tasks whose dependencies are deterministically satisfied."""
    try:
        ready = _plans(ctx).ready(plan_id, ctx.deps.session_id)
    except PlanNotFound as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "not_found", "message": str(exc)},
            "metadata": {},
        }
    return {"ok": True, "data": ready, "error": None, "metadata": {}}


async def plan_claim(
    ctx: RunContext[Any],
    plan_id: str,
    step_id: str,
    claimed_by: str,
    lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 900,
    justification: str = "",
) -> dict[str, Any]:
    """Claim one ready plan step with a durable lease and write-scope lock."""
    try:
        plan = _plans(ctx).claim(
            plan_id,
            step_id,
            session_id=ctx.deps.session_id,
            claimed_by=claimed_by,
            run_id=str(ctx.deps.run_id),
            lease_seconds=lease_seconds,
        )
    except (PlanNotFound, PlanConflict) as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "plan_conflict", "message": str(exc)},
            "metadata": {},
        }
    step = next(item for item in plan["steps"] if item["id"] == step_id)
    return {"ok": True, "data": step, "error": None, "metadata": {}}


async def evaluate_result(
    ctx: RunContext[Any],
    result: str,
    criteria: list[str],
    satisfied: list[str] | None = None,
    max_iterations: Annotated[int, Field(ge=1, le=10)] = 3,
    justification: str = "",
) -> dict[str, Any]:
    """Record a transparent structured self-evaluation without hidden reasoning."""
    if not criteria:
        raise ValueError("at least one criterion is required")
    declared = set(satisfied or [])
    checks = [{"criterion": item, "satisfied": item in declared} for item in criteria]
    missing = [item["criterion"] for item in checks if not item["satisfied"]]
    return {
        "ok": True,
        "data": {
            "checks": checks,
            "passed": not missing,
            "missing": missing,
            "result_preview": result[:2000],
        },
        "error": None,
        "metadata": {"max_iterations": max_iterations},
    }


async def session_status(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """Summarize durable event counts for the current session."""
    events = ctx.deps.events.read(ctx.deps.session_id)
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    return {
        "ok": True,
        "data": {"session_id": str(ctx.deps.session_id), "events": len(events), "types": counts},
        "error": None,
        "metadata": {},
    }


async def checkpoint_create(
    ctx: RunContext[Any], label: str, summary: str, justification: str = ""
) -> dict[str, Any]:
    """Create a durable explicit checkpoint without modifying model history."""
    checkpoint_id = str(uuid4())
    directory = ctx.deps.events.directory / "checkpoints" / str(ctx.deps.session_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{checkpoint_id}.json"
    payload = {
        "checkpoint_id": checkpoint_id,
        "session_id": str(ctx.deps.session_id),
        "label": label,
        "summary": summary,
        "created_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "data": payload, "error": None, "metadata": {"path": str(path)}}


async def trace_query(
    ctx: RunContext[Any],
    event_type: str | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    justification: str = "",
) -> dict[str, Any]:
    """Read bounded public audit events; private reasoning is never stored here."""
    events = ctx.deps.events.read(ctx.deps.session_id)
    if event_type:
        events = [event for event in events if event.type == event_type]
    selected = events[-limit:]
    return {
        "ok": True,
        "data": [event.model_dump(mode="json") for event in selected],
        "error": None,
        "metadata": {"total": len(events), "truncated": len(events) > limit},
    }


async def metrics_summary(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """Aggregate tool completion, failure, approval, and duration metrics."""
    events = ctx.deps.events.read(ctx.deps.session_id)
    completed = [event for event in events if event.type == "tool.completed"]
    failed = [event for event in events if event.type == "tool.failed"]
    durations = [
        float(event.payload["duration_ms"])
        for event in completed + failed
        if isinstance(event.payload.get("duration_ms"), (int, float))
    ]
    return {
        "ok": True,
        "data": {
            "tool_completed": len(completed),
            "tool_failed": len(failed),
            "approvals_requested": sum(event.type == "approval.requested" for event in events),
            "duration_ms_total": sum(durations),
            "duration_ms_average": sum(durations) / len(durations) if durations else 0,
        },
        "error": None,
        "metadata": {},
    }


async def cron_list(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """List persistent scheduled jobs and their latest runtime state."""
    jobs = [job.model_dump(mode="json") for job in _crons(ctx).list()]
    return {"ok": True, "data": jobs, "error": None, "metadata": {"count": len(jobs)}}


async def cron_create(
    ctx: RunContext[Any],
    name: str,
    schedule: str,
    prompt: str,
    agent_id: str = "main",
    enabled: bool = True,
    auto_resume: bool = True,
    justification: str = "",
) -> dict[str, Any]:
    """Create a persistent cron job scoped to the current workspace."""
    try:
        job = _crons(ctx).create(
            CronJobInput(
                name=name,
                schedule=schedule,
                prompt=prompt,
                workspace=ctx.deps.workspace,
                agent_id=agent_id,
                security_mode=ctx.deps.security_mode,
                provider_id=ctx.deps.provider_id,
                model=ctx.deps.model_name,
                enabled=enabled,
                auto_resume=auto_resume,
            )
        )
    except SchedulerError as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "validation", "message": str(exc)},
            "metadata": {},
        }
    return {"ok": True, "data": job.model_dump(mode="json"), "error": None, "metadata": {}}


async def cron_set_enabled(
    ctx: RunContext[Any],
    job_id: str,
    enabled: bool,
    justification: str = "",
) -> dict[str, Any]:
    """Enable or pause a persistent cron job."""
    service = _crons(ctx)
    try:
        current = service.get(job_id)
        payload = CronJobInput.model_validate(
            {
                **current.model_dump(
                    exclude={
                        "id",
                        "session_id",
                        "created_at",
                        "updated_at",
                        "next_run_at",
                        "last_run_at",
                        "last_status",
                        "last_error",
                        "in_flight",
                        "last_retryable",
                    }
                ),
                "enabled": enabled,
            }
        )
        job = service.update(job_id, payload)
    except SchedulerError as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "not_found", "message": str(exc)},
            "metadata": {},
        }
    return {"ok": True, "data": job.model_dump(mode="json"), "error": None, "metadata": {}}


async def cron_delete(ctx: RunContext[Any], job_id: str, justification: str = "") -> dict[str, Any]:
    """Delete one persistent cron definition, never its session audit."""
    try:
        _crons(ctx).delete(job_id)
    except SchedulerError as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "not_found", "message": str(exc)},
            "metadata": {},
        }
    return {"ok": True, "data": {"id": job_id, "status": "deleted"}, "error": None, "metadata": {}}


class OperationsModule:
    def toolsets(self):
        return [
            FunctionToolset(
                tools=[
                    plan_create,
                    plan_update,
                    plan_status,
                    plan_ready,
                    plan_claim,
                    evaluate_result,
                    session_status,
                    checkpoint_create,
                    trace_query,
                    metrics_summary,
                    cron_list,
                    cron_create,
                    cron_set_enabled,
                    cron_delete,
                ]
            )
        ]

    def instructions(self):
        return [
            "Use plan tools for multi-step work and keep plan states aligned with actual execution."
        ]

    def capabilities(self):
        return []


module = OperationsModule()
