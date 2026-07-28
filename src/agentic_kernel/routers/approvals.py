from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..kernel import Kernel
from ..models import ApprovalRequest, RunResult


class ApprovalResolveBody(BaseModel):
    approved: bool


class ApprovalBatchResolveBody(BaseModel):
    approval_ids: list[str] = Field(min_length=1)
    approved: bool


def create_approval_router(
    kernel: Kernel,
    running_tasks: dict[UUID, asyncio.Task[Any]],
) -> APIRouter:
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.get("", response_model=list[ApprovalRequest])
    async def list_approvals() -> list[ApprovalRequest]:
        return kernel.list_approvals()

    @router.post("/{approval_id}/resolve", response_model=RunResult)
    async def resolve_approval(
        approval_id: str, payload: ApprovalResolveBody
    ) -> RunResult:
        state = kernel.approvals.load_state(UUID(approval_id))
        session_id = UUID(str(state["approval"]["session_id"])) if state else None
        task = asyncio.create_task(kernel.resolve_approval(approval_id, payload.approved))
        if session_id:
            running_tasks[session_id] = task
        try:
            return await task
        finally:
            if session_id:
                running_tasks.pop(session_id, None)

    @router.post("/resolve-batch", response_model=RunResult)
    async def resolve_approval_batch(payload: ApprovalBatchResolveBody) -> RunResult:
        state = kernel.approvals.load_state(UUID(payload.approval_ids[0]))
        session_id = UUID(str(state["approval"]["session_id"])) if state else None
        task = asyncio.create_task(
            kernel.resolve_approval_batch(payload.approval_ids, payload.approved)
        )
        if session_id:
            running_tasks[session_id] = task
        try:
            return await task
        finally:
            if session_id:
                running_tasks.pop(session_id, None)

    return router
