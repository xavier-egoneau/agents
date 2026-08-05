import sqlite3
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


def test_delegated_step_requires_parent_validation_evidence(tmp_path) -> None:
    session_id = uuid4()
    service = PlanService(tmp_path / "state.db")
    plan = service.create(
        session_id,
        "Delegated",
        [
            PlanStepInput(
                id="T1",
                title="Implement component",
                parallelizable=True,
                write_scopes=["src/component"],
            )
        ],
    )
    service.claim(
        plan["plan_id"],
        "T1",
        session_id=session_id,
        claimed_by="subagent:frontend",
        run_id="child-run",
    )
    service.update(
        plan["plan_id"],
        "T1",
        "validating",
        session_id=session_id,
        note="Subagent returned a result.",
    )

    with pytest.raises(PlanConflict, match="plan_validate"):
        service.update(
            plan["plan_id"],
            "T1",
            "completed",
            session_id=session_id,
        )
    with pytest.raises(PlanConflict, match="evidence"):
        service.validate(
            plan["plan_id"],
            "T1",
            session_id=session_id,
            passed=True,
            evidence=[],
        )

    validated = service.validate(
        plan["plan_id"],
        "T1",
        session_id=session_id,
        passed=True,
        evidence=["pytest tests/component", "artifact:screen-123"],
        note="Tests pass and screenshot inspected.",
    )
    step = validated["steps"][0]
    assert step["status"] == "completed"
    assert step["validation"]["passed"] is True
    assert step["validation"]["evidence"] == [
        "pytest tests/component",
        "artifact:screen-123",
    ]


def test_plan_schema_adds_validation_column_non_destructively(tmp_path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE plan_steps (
                plan_id TEXT NOT NULL, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                title TEXT NOT NULL, status TEXT NOT NULL, dependencies_json TEXT NOT NULL,
                parallelizable INTEGER NOT NULL, write_scopes_json TEXT NOT NULL,
                note TEXT, claimed_by TEXT, claimed_at TEXT, lease_until TEXT, run_id TEXT,
                PRIMARY KEY(plan_id, step_id)
            )"""
        )
        db.execute(
            """CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                steps_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )

    with PlanService(database)._db() as db:
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(plan_steps)").fetchall()
        }
    assert "validation_json" in columns
