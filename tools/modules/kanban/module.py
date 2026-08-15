from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.kanban import (
    KanbanError,
    KanbanService,
    KanbanTaskInput,
    KanbanTaskPatch,
)


def _service(ctx: RunContext[Any]) -> KanbanService:
    state_db = Path(ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db"))
    return KanbanService(state_db)


def _workspace(ctx: RunContext[Any]) -> str:
    return str(Path(ctx.deps.workspace).resolve())


def _success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": {}}


def _failure(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"type": "kanban", "message": str(exc), "retryable": False},
        "metadata": {},
    }


async def kanban_list(
    ctx: RunContext[Any],
    status: Literal["backlog", "ready", "running", "review", "done", "blocked"] | None = None,
    automatic_only: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """List Kanban tasks belonging to the current project workspace."""
    tasks = _service(ctx).list(_workspace(ctx))
    if status:
        tasks = [task for task in tasks if task["status"] == status]
    if automatic_only:
        tasks = [task for task in tasks if task["auto_run"]]
    return _success({"tasks": tasks, "count": len(tasks), "workspace": _workspace(ctx)})


async def kanban_create(
    ctx: RunContext[Any],
    title: Annotated[str, Field(min_length=1, max_length=240)],
    prompt: str = "",
    acceptance_criteria: str = "",
    status: Literal["backlog", "ready"] = "backlog",
    priority: Literal["low", "medium", "high", "urgent"] = "medium",
    auto_run: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Create a task on the current project board."""
    try:
        task = _service(ctx).create(
            KanbanTaskInput(
                title=title,
                workspace=_workspace(ctx),
                prompt=prompt,
                acceptance_criteria=acceptance_criteria,
                status=status,
                priority=priority,
                auto_run=auto_run,
                agent_id=str(getattr(ctx.deps, "orchestrator_id", "main") or "main"),
            )
        )
        return _success(task)
    except KanbanError as exc:
        return _failure(exc)


async def kanban_update(
    ctx: RunContext[Any],
    task_id: str,
    status: Literal["backlog", "ready", "running", "review", "done", "blocked"] | None = None,
    prompt: str | None = None,
    acceptance_criteria: str | None = None,
    auto_run: bool | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Update the execution content or state of a Kanban task."""
    try:
        return _success(
            _service(ctx).update(
                task_id,
                KanbanTaskPatch(
                    status=status,
                    prompt=prompt,
                    acceptance_criteria=acceptance_criteria,
                    auto_run=auto_run,
                ),
            )
        )
    except KanbanError as exc:
        return _failure(exc)


async def kanban_claim_next(
    ctx: RunContext[Any],
    worker_id: str = "agent",
    lease_seconds: Annotated[int, Field(ge=30, le=86400)] = 1800,
    automatic_only: bool = True,
    justification: str = "",
) -> dict[str, Any]:
    """Atomically reserve the next ready task whose dependencies are done."""
    task = _service(ctx).claim_next(
        _workspace(ctx), worker_id, lease_seconds=lease_seconds, automatic_only=automatic_only
    )
    return _success({"task": task, "claimed": task is not None})


async def kanban_finish(
    ctx: RunContext[Any],
    task_id: str,
    status: Literal["review", "done", "blocked"] = "review",
    result: str = "",
    error: str = "",
    justification: str = "",
) -> dict[str, Any]:
    """Release a claimed task and record its outcome."""
    try:
        return _success(_service(ctx).finish(task_id, status, result=result, error=error))
    except KanbanError as exc:
        return _failure(exc)


class KanbanModule:
    def toolsets(self):
        return [
            FunctionToolset(
                tools=[kanban_list, kanban_create, kanban_update, kanban_claim_next, kanban_finish]
            )
        ]

    def instructions(self):
        return [
            "The Kanban is the durable source of project work. For automated work, call "
            "kanban_claim_next before acting, execute exactly the claimed prompt in the current "
            "workspace, then call kanban_finish with review, done, or blocked. Never work on an "
            "unclaimed automatic task and never claim a second task while one is running."
        ]

    def capabilities(self):
        return []


module = KanbanModule()
