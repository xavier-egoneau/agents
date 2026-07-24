from uuid import uuid4

import pytest

from agentic_kernel.plans import (
    PlanConflict,
    PlanError,
    PlanService,
    PlanStepInput,
)


def test_plan_service_validates_graph_and_ready_waves(tmp_path) -> None:
    session_id = uuid4()
    service = PlanService(tmp_path / "state.db")
    plan = service.create(
        session_id,
        "Build",
        [
            PlanStepInput(id="T1", title="Foundation"),
            PlanStepInput(
                id="T2",
                title="Frontend",
                dependencies=["T1"],
                parallelizable=True,
            ),
            PlanStepInput(
                id="T3",
                title="Backend",
                dependencies=["T1"],
                parallelizable=True,
            ),
            PlanStepInput(
                id="T4",
                title="Integration",
                dependencies=["T2", "T3"],
            ),
        ],
    )
    first = service.ready(plan["plan_id"], session_id)
    assert [step["id"] for step in first["sequential"]] == ["T1"]
    with pytest.raises(PlanConflict):
        service.update(
            plan["plan_id"], "T2", "in_progress", session_id=session_id
        )
    service.update(plan["plan_id"], "T1", "completed", session_id=session_id)
    second = service.ready(plan["plan_id"], session_id)
    assert [step["id"] for step in second["parallel"]] == ["T2", "T3"]


def test_plan_service_rejects_cycles(tmp_path) -> None:
    service = PlanService(tmp_path / "state.db")
    with pytest.raises(PlanError, match="cyclic"):
        service.create(
            uuid4(),
            "Cycle",
            [
                PlanStepInput(id="T1", title="One", dependencies=["T2"]),
                PlanStepInput(id="T2", title="Two", dependencies=["T1"]),
            ],
        )
