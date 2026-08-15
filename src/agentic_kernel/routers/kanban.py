from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..kanban import (
    KanbanConflict,
    KanbanNotFound,
    KanbanService,
    KanbanTaskInput,
    KanbanTaskPatch,
)


class ClaimBody(BaseModel):
    workspace: str = Field(min_length=1)
    worker_id: str = Field(min_length=1, max_length=200)
    lease_seconds: int = Field(default=1800, ge=30, le=86400)
    automatic_only: bool = True


class FinishBody(BaseModel):
    status: str = Field(pattern=r"^(review|done|blocked)$")
    result: str = ""
    error: str = ""
    run_id: str | None = None
    session_id: str | None = None


def create_kanban_router(database: Path) -> APIRouter:  # noqa: C901 - route assembly
    router = APIRouter(prefix="/api/kanban", tags=["kanban"])
    service = KanbanService(database)

    @router.get("/tasks")
    async def tasks(workspace: str = Query(min_length=1)) -> list[dict]:
        return service.list(workspace)

    @router.post("/tasks", status_code=201)
    async def create_task(payload: KanbanTaskInput) -> dict:
        return service.create(payload)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        try:
            return service.get(task_id)
        except KanbanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/tasks/{task_id}")
    async def update_task(task_id: str, payload: KanbanTaskPatch) -> dict:
        try:
            return service.update(task_id, payload)
        except KanbanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KanbanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete("/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: str) -> None:
        try:
            service.delete(task_id)
        except KanbanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/claim-next")
    async def claim_next(payload: ClaimBody) -> dict | None:
        return service.claim_next(
            payload.workspace,
            payload.worker_id,
            lease_seconds=payload.lease_seconds,
            automatic_only=payload.automatic_only,
        )

    @router.post("/tasks/{task_id}/finish")
    async def finish(task_id: str, payload: FinishBody) -> dict:
        try:
            return service.finish(
                task_id,
                payload.status,  # type: ignore[arg-type]
                result=payload.result,
                error=payload.error,
                run_id=payload.run_id,
                session_id=payload.session_id,
            )
        except KanbanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
