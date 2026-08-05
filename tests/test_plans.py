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


def _deps(state_db, session_id):
    """Dépendances minimales pour exercer la délégation hors d'un vrai run."""
    import asyncio

    from agentic_kernel.events import JsonlEventStore
    from agentic_kernel.models import BudgetConfig
    from agentic_kernel.orchestration import RuntimeDeps

    return RuntimeDeps(
        session_id=session_id,
        root_run_id=uuid4(),
        budgets=BudgetConfig(),
        events=JsonlEventStore(state_db.parent / "sessions"),
        semaphore=asyncio.Semaphore(4),
        state_db=state_db,
    )


def _plan_with_two_parallel_steps(state_db, session_id):
    service = PlanService(state_db)
    plan = service.create(
        session_id,
        "Build",
        [
            PlanStepInput(id="T1", title="Socle"),
            PlanStepInput(
                id="T2", title="Front", dependencies=["T1"],
                parallelizable=True, write_scopes=["src/front"],
            ),
            PlanStepInput(
                id="T3", title="Front bis", dependencies=["T1"],
                parallelizable=True, write_scopes=["src/front"],
            ),
        ],
    )
    return service, plan["plan_id"]


def test_a_named_agent_can_claim_a_plan_step(tmp_path) -> None:
    """Le mécanisme n'était atteignable que par l'exécutant neutre.

    Les agents configurés — outillés et relus — restaient hors du plan : pour
    paralléliser, il fallait renoncer à eux.
    """
    from agentic_kernel.orchestration import TracedSubAgentToolset

    state_db = tmp_path / "state.db"
    session_id = uuid4()
    service, plan_id = _plan_with_two_parallel_steps(state_db, session_id)
    service.update(plan_id, "T1", "completed", session_id=session_id)

    TracedSubAgentToolset._claim_plan_step(
        _deps(state_db, session_id), "uifront", plan_id, "T2", uuid4()
    )

    etape = next(s for s in service.get(plan_id, session_id)["steps"] if s["id"] == "T2")
    assert etape["status"] == "claimed"
    assert etape["claimed_by"] == "agent:uifront"


def test_overlapping_write_scopes_are_refused(tmp_path) -> None:
    """Deux agents ne peuvent pas écrire au même endroit en même temps."""
    from pydantic_ai import ModelRetry

    from agentic_kernel.orchestration import TracedSubAgentToolset

    state_db = tmp_path / "state.db"
    session_id = uuid4()
    service, plan_id = _plan_with_two_parallel_steps(state_db, session_id)
    service.update(plan_id, "T1", "completed", session_id=session_id)
    deps = _deps(state_db, session_id)
    TracedSubAgentToolset._claim_plan_step(deps, "uifront", plan_id, "T2", uuid4())

    # T3 écrit dans le même périmètre que T2, déjà réservée.
    with pytest.raises(ModelRetry, match="T3"):
        TracedSubAgentToolset._claim_plan_step(deps, "dev", plan_id, "T3", uuid4())


def test_delegation_without_a_plan_stays_possible(tmp_path) -> None:
    """Déléguer hors plan reste le cas courant : aucun identifiant requis."""
    from agentic_kernel.orchestration import TracedSubAgentToolset

    TracedSubAgentToolset._claim_plan_step(
        _deps(tmp_path / "state.db", uuid4()), "researcher", None, None, uuid4()
    )


def test_a_half_given_plan_reference_is_reported(tmp_path) -> None:
    from pydantic_ai import ModelRetry

    from agentic_kernel.orchestration import TracedSubAgentToolset

    with pytest.raises(ModelRetry, match="ensemble"):
        TracedSubAgentToolset._claim_plan_step(
            _deps(tmp_path / "state.db", uuid4()), "dev", "plan-1", None, uuid4()
        )
