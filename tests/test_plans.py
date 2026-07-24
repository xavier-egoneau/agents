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
                write_scopes=["src/frontend"],
            ),
            PlanStepInput(
                id="T3",
                title="Backend",
                dependencies=["T1"],
                parallelizable=True,
                write_scopes=["src/backend"],
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
        service.update(plan["plan_id"], "T2", "in_progress", session_id=session_id)
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


def test_plan_claims_distinct_scopes_and_rejects_overlap(tmp_path) -> None:
    session_id = uuid4()
    service = PlanService(tmp_path / "state.db")
    plan = service.create(
        session_id,
        "Parallel",
        [
            PlanStepInput(
                id="T1",
                title="Frontend",
                parallelizable=True,
                write_scopes=["src/frontend"],
            ),
            PlanStepInput(
                id="T2",
                title="Backend",
                parallelizable=True,
                write_scopes=["src/backend"],
            ),
            PlanStepInput(
                id="T3",
                title="Shared UI",
                parallelizable=True,
                write_scopes=["src/frontend/components"],
            ),
        ],
    )
    service.claim(
        plan["plan_id"],
        "T1",
        session_id=session_id,
        claimed_by="worker-a",
        run_id="run-a",
    )
    service.claim(
        plan["plan_id"],
        "T2",
        session_id=session_id,
        claimed_by="worker-b",
        run_id="run-b",
    )
    with pytest.raises(PlanConflict, match="overlaps"):
        service.claim(
            plan["plan_id"],
            "T3",
            session_id=session_id,
            claimed_by="worker-c",
            run_id="run-c",
        )


def test_parallel_step_requires_write_scope(tmp_path) -> None:
    with pytest.raises(PlanError, match="write scope"):
        PlanService(tmp_path / "state.db").create(
            uuid4(),
            "Unsafe parallel plan",
            [PlanStepInput(id="T1", title="Task", parallelizable=True)],
        )


def test_expired_claim_can_be_recovered_after_crash(tmp_path) -> None:
    session_id = uuid4()
    service = PlanService(tmp_path / "state.db")
    plan = service.create(
        session_id,
        "Recovery",
        [PlanStepInput(id="T1", title="Recoverable")],
    )
    service.claim(
        plan["plan_id"],
        "T1",
        session_id=session_id,
        claimed_by="crashed-worker",
        run_id="old-run",
        lease_seconds=-1,
    )
    ready = service.ready(plan["plan_id"], session_id)
    assert [step["id"] for step in ready["ready"]] == ["T1"]
