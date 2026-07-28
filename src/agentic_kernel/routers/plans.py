from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..events import JsonlEventStore
from ..models import Event
from ..plans import PlanConflict, PlanNotFound, PlanService


class PlanStepStatusBody(BaseModel):
    status: str = Field(
        pattern=r"^(pending|claimed|in_progress|validating|completed|blocked|failed)$"
    )


def create_plan_router(database: Path, events: JsonlEventStore) -> APIRouter:
    router = APIRouter(prefix="/api/plans", tags=["plans"])
    plans = PlanService(database)

    @router.get("/current")
    async def current_plan(session_id: UUID) -> dict[str, object] | None:
        plan = plans.current(session_id)
        if plan is None:
            return None
        # Completed plans remain archived in SQLite but leave active composer state.
        if plan["steps"] and all(step["status"] == "completed" for step in plan["steps"]):
            return None
        return plan

    @router.patch("/{plan_id}/steps/{step_id}")
    async def set_plan_step(
        plan_id: str, step_id: str, payload: PlanStepStatusBody
    ) -> dict[str, object]:
        try:
            plan = plans.update(plan_id, step_id, payload.status)
        except PlanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        events.append(
            Event(
                session_id=UUID(plan["session_id"]),
                run_id=uuid4(),
                agent_id="user",
                type="plan.updated",
                payload={
                    "plan_id": plan_id,
                    "step_id": step_id,
                    "status": payload.status,
                    "source": "web",
                },
            )
        )
        return plan

    @router.delete("/{plan_id}")
    async def delete_plan(plan_id: str) -> dict[str, str]:
        try:
            plan = plans.delete(plan_id)
        except PlanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        events.append(
            Event(
                session_id=UUID(plan["session_id"]),
                run_id=uuid4(),
                agent_id="user",
                type="plan.deleted",
                payload={"plan_id": plan_id, "source": "web"},
            )
        )
        return {"plan_id": plan_id, "status": "deleted"}

    return router
