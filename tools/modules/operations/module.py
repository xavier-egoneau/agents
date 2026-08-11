from __future__ import annotations

import json
import re
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


async def plan_validate(
    ctx: RunContext[Any],
    plan_id: str,
    step_id: str,
    passed: bool,
    evidence: list[str],
    note: str | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Validate a delegated result from explicit parent checks and evidence."""
    try:
        plan = _plans(ctx).validate(
            plan_id,
            step_id,
            session_id=ctx.deps.session_id,
            passed=passed,
            evidence=evidence,
            note=note,
        )
    except (PlanNotFound, PlanConflict) as exc:
        return {
            "ok": False,
            "data": None,
            "error": {"type": "validation_failed", "message": str(exc)},
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


_AUTO_LIVRAISON = re.compile(
    r"api\.telegram\.org|telegram_[a-z0-9_]*_(?:bot_token|user_id)|bot_token",
    re.IGNORECASE,
)


def _refabrique_la_livraison(prompt: str) -> bool:
    """Détecte un prompt qui tente d'expédier le message lui-même.

    La détection vise les identifiants de la messagerie de l'agent — jeton de
    bot, hôte de l'API. Un prompt qui les nomme ne peut pas viser autre chose
    que sa propre livraison, alors qu'elle est déjà assurée. On ne refuse pas
    « appeler une API » en général : un webhook métier reste légitime.
    """
    return bool(_AUTO_LIVRAISON.search(prompt))


def _refus(message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"type": "validation", "message": message},
            "metadata": {}}


async def cron_list(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """List persistent scheduled jobs and their latest runtime state."""
    jobs = [job.model_dump(mode="json") for job in _crons(ctx).list()]
    return {"ok": True, "data": jobs, "error": None, "metadata": {"count": len(jobs)}}


async def cron_create(
    ctx: RunContext[Any],
    name: str,
    prompt: str,
    schedule: str = "",
    one_shot_at: str = "",
    kind: str = "",
    agent_id: str = "main",
    enabled: bool = True,
    auto_resume: bool = True,
    justification: str = "",
) -> dict[str, Any]:
    """Schedule work: a recurring routine, or a reminder that fires once.

    Give exactly one of the two.

    `schedule` is a five-field cron expression, for work that repeats:
    `0 9 * * *` every morning at nine, `0 8 * * 1` every Monday.

    `one_shot_at` is an ISO 8601 datetime for something that must happen once
    and never again — a reminder, a deadline, a follow-up on a precise date.
    Example: `2026-08-11T07:00:00+02:00`. Include the offset, or the local
    timezone of the kernel is assumed.

    A reminder expressed as cron would come back every year: `0 7 11 8 *` fires
    each 11 August. Use `one_shot_at` for anything the user described as
    happening on a given date.

    `kind` decides whether a model runs at all.

    Use `kind="reminder"` when the message is already known now — a reminder, an
    alert, a note to self. `prompt` is then the message itself, delivered
    verbatim without invoking any model: no tokens, no latency, and it still
    arrives if the provider is down. Write the text the user should read:
    "Rendez-vous médecin à 13h40", not "envoie un message pour rappeler...".

    Use `kind="agent"` when the content depends on the state of the world at
    trigger time — a watch, a summary, "remind me to look at the open PRs".
    `prompt` is then the request made to the agent, and its answer is delivered.

    In both cases delivery is the kernel's job: the answer reaches the user's
    canonical session and Telegram. Never send it yourself.
    """
    if kind not in {"agent", "reminder"}:
        return _refus(
            "`kind` est obligatoire : 'reminder' si le message est déjà connu "
            "maintenant, 'agent' s'il dépend de l'état du monde au déclenchement. "
            f"Reçu {kind!r}."
        )
    # Garde-fou : une description, aussi claire soit-elle, se laisse ignorer.
    # Trois routines de suite ont été créées avec un prompt qui refabriquait la
    # livraison — appel à l'API Telegram, jeton nommé en clair — alors qu'elle
    # est automatique. Le refus, lui, ne se laisse pas ignorer.
    if _refabrique_la_livraison(prompt):
        return _refus(
            "Ce prompt envoie le message lui-même. La livraison est automatique : "
            "la réponse d'une routine atteint la session canonique de l'agent et "
            "son Telegram. Utilise `kind='reminder'` avec le texte du message "
            "comme `prompt` — « Rendez-vous médecin à 13h40 » — sans consigne "
            "d'envoi ni jeton."
        )
    if bool(schedule.strip()) == bool(one_shot_at.strip()):
        return _refus(
            "Fournir soit `schedule` pour une répétition, soit `one_shot_at` "
            "pour une occurrence unique — exactement l'un des deux."
        )
    try:
        moment = datetime.fromisoformat(one_shot_at) if one_shot_at.strip() else None
    except ValueError:
        return _refus(
            f"Date invalide : {one_shot_at!r}. Format attendu ISO 8601, "
            "par exemple 2026-08-11T07:00:00+02:00."
        )
    try:
        job = _crons(ctx).create(
            CronJobInput(
                name=name,
                schedule=schedule.strip(),
                one_shot_at=moment,
                prompt=prompt,
                workspace=ctx.deps.workspace,
                agent_id=agent_id,
                security_mode=ctx.deps.security_mode,
                provider_id=ctx.deps.provider_id,
                model=ctx.deps.model_name,
                enabled=enabled,
                auto_resume=auto_resume,
                kind=kind,
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
                    plan_validate,
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
            "Use plan tools for multi-step work and keep plan states aligned with actual "
            "execution.",
            # Sans cette précision, l'agent reconstruit une livraison : appel HTTP
            # à l'API Telegram, jeton nommé dans le prompt, autorisation réseau à
            # accorder. L'utilisateur demandait un rappel et se retrouve à valider
            # une requête sortante. La livraison existe déjà, en dessous de lui.
            "A scheduled job's final answer is delivered to the user automatically: it is "
            "posted to the agent's canonical session and pushed to Telegram when the agent "
            "has it configured. Never build your own delivery — no http_request to a "
            "messaging API, no bot token in the prompt. Write the routine prompt so that the "
            "answer *is* the message the user should receive.",
        ]

    def capabilities(self):
        return []


module = OperationsModule()
