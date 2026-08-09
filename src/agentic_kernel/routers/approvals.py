from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..errors import ConfigurationError
from ..kernel import Kernel
from ..models import ApprovalRequest, RunRequest, RunResult
from ..scheduler import CronService


class ApprovalResolveBody(BaseModel):
    approved: bool


class ApprovalBatchResolveBody(BaseModel):
    approval_ids: list[str] = Field(min_length=1)
    approved: bool


def create_approval_router(  # noqa: C901 - dette: factory à plusieurs endpoints
    kernel: Kernel,
    running_tasks: dict[UUID, asyncio.Task[Any]],
    cron_service: CronService | None = None,
    deliver_cron_result: Callable[[RunRequest, RunResult], None] | None = None,
    validate_cron_request: Callable[[RunRequest], None] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("", response_model=list[ApprovalRequest])
    async def list_approvals() -> list[ApprovalRequest]:
        return kernel.list_approvals()

    @router.post("/{approval_id}/resolve", response_model=RunResult)
    async def resolve_approval(  # noqa: C901 - dette: résolution multi-cas
        approval_id: str, payload: ApprovalResolveBody
    ) -> RunResult:
        try:
            approval_uuid = UUID(approval_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Autorisation invalide") from exc
        state = kernel.approvals.load_state(approval_uuid)
        if state is None:
            raise HTTPException(status_code=404, detail="Autorisation introuvable ou déjà traitée")
        session_id = UUID(str(state["approval"]["session_id"])) if state else None
        request = RunRequest.model_validate(state["request"])
        if payload.approved and validate_cron_request and request.cron_job_id:
            try:
                validate_cron_request(request)
            except (ConfigurationError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if session_id and session_id in running_tasks:
            raise HTTPException(status_code=409, detail="Une résolution est déjà en cours")
        task = asyncio.create_task(kernel.resolve_approval(approval_id, payload.approved))
        if session_id:
            running_tasks[session_id] = task
        try:
            try:
                result = await task
            except (ConfigurationError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if cron_service and session_id:
                if request.trigger == "cron_test" and request.cron_job_id:
                    cron_service.record_test_result(request.cron_job_id, result)
                elif request.cron_occurrence_id:
                    cron_service.resume_occurrence(request.cron_occurrence_id, result)
                else:
                    cron_service.resume_for_session(
                        session_id, result, cron_job_id=request.cron_job_id
                    )
            if deliver_cron_result:
                deliver_cron_result(request, result)
            return result
        finally:
            if session_id:
                running_tasks.pop(session_id, None)

    @router.post("/resolve-batch", response_model=RunResult)
    async def resolve_approval_batch(  # noqa: C901 - dette: résolution par lot multi-cas
        payload: ApprovalBatchResolveBody,
    ) -> RunResult:
        try:
            first_id = UUID(payload.approval_ids[0])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Autorisation invalide") from exc
        state = kernel.approvals.load_state(first_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail="Autorisations introuvables ou déjà traitées",
            )
        session_id = UUID(str(state["approval"]["session_id"])) if state else None
        request = RunRequest.model_validate(state["request"])
        if payload.approved and validate_cron_request and request.cron_job_id:
            try:
                validate_cron_request(request)
            except (ConfigurationError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if session_id and session_id in running_tasks:
            raise HTTPException(status_code=409, detail="Une résolution est déjà en cours")
        task = asyncio.create_task(
            kernel.resolve_approval_batch(payload.approval_ids, payload.approved)
        )
        if session_id:
            running_tasks[session_id] = task
        try:
            try:
                result = await task
            except (ConfigurationError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if cron_service and session_id:
                if request.trigger == "cron_test" and request.cron_job_id:
                    cron_service.record_test_result(request.cron_job_id, result)
                elif request.cron_occurrence_id:
                    cron_service.resume_occurrence(request.cron_occurrence_id, result)
                else:
                    cron_service.resume_for_session(
                        session_id, result, cron_job_id=request.cron_job_id
                    )
            if deliver_cron_result:
                deliver_cron_result(request, result)
            return result
        finally:
            if session_id:
                running_tasks.pop(session_id, None)

    return router
