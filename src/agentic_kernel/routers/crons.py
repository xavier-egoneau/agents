from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ..models import RunResult
from ..scheduler import CronJobInput, CronScheduler, CronService, SchedulerError
from .runs import LaunchRun


class CronJobBody(CronJobInput):
    pass


def create_cron_router(
    service: CronService,
    scheduler: CronScheduler,
    launch: LaunchRun,
) -> APIRouter:
    router = APIRouter(prefix="/api/crons", tags=["automations"])

    @router.get("")
    async def list_crons() -> list[dict[str, object]]:
        return [job.model_dump(mode="json") for job in service.list()]

    @router.post("")
    async def create_cron(payload: CronJobBody) -> dict[str, object]:
        try:
            return service.create(payload).model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/{job_id}")
    async def update_cron(job_id: str, payload: CronJobBody) -> dict[str, object]:
        try:
            return service.update(job_id, payload).model_dump(mode="json")
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
        if job.in_flight:
            raise HTTPException(status_code=409, detail="Ce cronjob est déjà en cours")
        claimed = service.claim(job_id)
        if claimed is None:
            raise HTTPException(status_code=409, detail="Cronjob désactivé ou déjà en cours")
        task = asyncio.create_task(scheduler._execute(claimed), name=f"amk-cron-{job_id}")
        scheduler._tasks.add(task)
        task.add_done_callback(scheduler._tasks.discard)
        return {"id": job_id, "status": "started", "session_id": str(job.session_id)}

    @router.post("/{job_id}/test", response_model=RunResult)
    async def test_cron(job_id: str) -> RunResult:
        """Test without advancing schedule, preserving exact guardian grants."""
        try:
            job = service.get(job_id)
            return await launch(CronScheduler.request_for(job, trigger="cron_test"))
        except SchedulerError as exc:
            status = 404 if "introuvable" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    return router
