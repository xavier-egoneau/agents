from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from ..kernel import Kernel
from ..models import Event, RunResult
from ..scheduler import CronJobInput, CronScheduler, CronService, SchedulerError
from .runs import LaunchRun


class CronJobBody(CronJobInput):
    pass


def create_cron_router(
    service: CronService,
    scheduler: CronScheduler,
    launch: LaunchRun,
    kernel: Kernel,
) -> APIRouter:
    router = APIRouter(prefix="/api/crons", tags=["automations"])

    @router.get("")
    async def list_crons() -> list[dict[str, object]]:
        return [job.model_dump(mode="json") for job in service.list()]

    @router.get("/{job_id}/approval-status")
    async def cron_approval_status(job_id: str) -> dict[str, object]:
        try:
            job = service.get(job_id)
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        pending = [
            item for item in kernel.list_approvals() if item.session_id == job.session_id
        ]
        latest_run_id = (
            str(max(pending, key=lambda item: item.created_at).run_id) if pending else None
        )
        latest_pending = [
            item for item in pending if latest_run_id and str(item.run_id) == latest_run_id
        ]
        scopes = sorted(
            kernel.approval_service.approved_scopes(job.session_id),
            key=lambda item: (item[0], item[1], item[2] or ""),
        )
        return {
            "approved_scopes": [
                {"tool_name": tool, "action_family": family, "path": path}
                for tool, family, path in scopes
            ],
            "pending_count": len(latest_pending),
            "pending_run_id": latest_run_id,
        }

    @router.get("/runs")
    async def list_cron_runs(
        job_id: str | None = None,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in service.list_runs(
                job_id=job_id,
                unread_only=unread_only,
                limit=limit,
            )
        ]

    @router.post("/runs/{occurrence_id}/read")
    async def mark_cron_run_read(occurrence_id: str) -> dict[str, object]:
        try:
            return service.mark_run_read(occurrence_id).model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/runs/read-session/{session_id}")
    async def mark_session_runs_read(session_id: str) -> dict[str, object]:
        from uuid import UUID

        try:
            count = service.mark_session_runs_read(UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Session invalide") from exc
        return {"session_id": session_id, "read": count}

    @router.post("")
    async def create_cron(payload: CronJobBody) -> dict[str, object]:
        try:
            return service.create(payload).model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/{job_id}")
    async def update_cron(job_id: str, payload: CronJobBody) -> dict[str, object]:
        try:
            previous = service.get(job_id)
            updated = service.update(job_id, payload)
            permission_changed = any(
                (
                    previous.prompt != updated.prompt,
                    previous.workspace != updated.workspace,
                    previous.agent_id != updated.agent_id,
                    previous.skills != updated.skills,
                    previous.security_mode != updated.security_mode,
                    previous.provider_id != updated.provider_id,
                    previous.model != updated.model,
                    previous.reasoning != updated.reasoning,
                )
            )
            if permission_changed:
                kernel.events.append(
                    Event(
                        session_id=updated.session_id,
                        run_id=uuid4(),
                        agent_id=updated.agent_id,
                        type="approval.grants.revoked",
                        payload={"cron_job_id": job_id, "reason": "configuration_changed"},
                    )
                )
            return updated.model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(
                status_code=404 if "introuvable" in str(exc) else 422,
                detail=str(exc),
            ) from exc

    @router.delete("/{job_id}")
    async def delete_cron(job_id: str) -> dict[str, str]:
        try:
            service.delete(job_id)
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": job_id, "status": "deleted"}

    @router.post("/{job_id}/run")
    async def run_cron_now(job_id: str) -> dict[str, str]:
        try:
            job = service.get(job_id)
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if job.in_flight or job.blocked:
            detail = (
                "Cette routine attend une autorisation"
                if job.blocked
                else "Ce cronjob est déjà en cours"
            )
            raise HTTPException(status_code=409, detail=detail)
        claimed = service.claim(job_id)
        if claimed is None:
            raise HTTPException(status_code=409, detail="Cronjob désactivé ou déjà en cours")
        task = asyncio.create_task(scheduler._execute(claimed), name=f"amk-cron-{job_id}")
        scheduler._tasks.add(task)
        task.add_done_callback(scheduler._tasks.discard)
        return {
            "id": job_id,
            "status": "started",
            "session_id": str(job.session_id),
            "notification_session_id": str(job.notification_session_id),
            "occurrence_id": claimed.occurrence_id or "",
        }

    @router.post("/{job_id}/test", response_model=RunResult)
    async def test_cron(job_id: str) -> RunResult:
        """Test without advancing schedule, preserving exact guardian grants."""
        try:
            job = service.get(job_id)
            result = await launch(CronScheduler.request_for(job, trigger="cron_test"))
            # A test does not claim or advance the schedule, but it is still the
            # latest diagnostic result and must clear an obsolete last_error.
            service.record_test_result(job_id, result)
            return result
        except SchedulerError as exc:
            status = 404 if "introuvable" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    return router
