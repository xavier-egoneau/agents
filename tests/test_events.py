import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import Event
from agentic_kernel.projections import SessionProjection


def test_jsonl_is_append_only_and_replayable(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    session_id = uuid4()
    root = uuid4()
    child = uuid4()
    store.append(Event(session_id=session_id, run_id=root, agent_id="main", type="started"))
    store.append(
        Event(
            session_id=session_id,
            run_id=child,
            parent_run_id=root,
            agent_id="child",
            type="failed",
            payload={"partial": "work"},
        )
    )
    events = store.read(session_id)
    assert [event.type for event in events] == ["started", "failed"]
    assert events[1].parent_run_id == root
    assert store.list_session_ids() == [session_id]


def test_sqlite_projection_paginates_messages_without_replaying_journal(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "sessions")
    session_id = uuid4()
    for index in range(6):
        run_id = uuid4()
        store.append(
            Event(
                session_id=session_id,
                run_id=run_id,
                agent_id="main",
                type="session.started",
                payload={"prompt": f"question {index}", "workspace": str(tmp_path)},
            )
        )
        store.append(
            Event(
                session_id=session_id,
                run_id=run_id,
                agent_id="main",
                type="session.completed",
                payload={"status": "success", "output": f"answer {index}"},
            )
        )

    latest = store.projection.messages(session_id, limit=4)
    assert [message["content"] for message in latest] == [
        "question 4",
        "answer 4",
        "question 5",
        "answer 5",
    ]
    older = store.projection.messages(
        session_id,
        limit=4,
        before_sequence=latest[0]["sequence"],
    )
    assert [message["content"] for message in older] == [
        "question 2",
        "answer 2",
        "question 3",
        "answer 3",
    ]


def test_old_cumulative_context_measurement_is_migrated_and_invalidated(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    session_id = str(uuid4())
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE projected_context (
                session_id TEXT PRIMARY KEY,
                estimated_history_tokens INTEGER NOT NULL DEFAULT 0,
                observed_input_tokens INTEGER,
                compaction_count INTEGER NOT NULL DEFAULT 0,
                calibration_factor REAL NOT NULL DEFAULT 1.0,
                calibration_samples INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """INSERT INTO projected_context VALUES
               (?, 12000, 714906, 466, 2.88, 7, '2026-08-12')""",
            (session_id,),
        )

    projection = SessionProjection(database)
    with projection._db() as db:  # noqa: SLF001 - vérifie la migration SQLite
        row = db.execute(
            "SELECT * FROM projected_context WHERE session_id=?", (session_id,)
        ).fetchone()

    assert row is not None
    assert row["observed_input_tokens"] is None
    assert row["last_run_total_input_tokens"] == 714_906
    assert row["calibration_factor"] == 1.0
    assert row["calibration_samples"] == 0
    assert row["calibration_version"] == 2


def test_projection_calibrates_only_from_one_comparable_request(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "sessions")
    session_id = uuid4()
    run_id = uuid4()
    store.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="messages.snapshot",
            payload={
                "estimated_tokens": 15_000,
                "estimated_latest_request_tokens": 10_000,
                "latest_request_input_tokens": 24_000,
            },
        )
    )

    context = store.projection.context(session_id)

    assert context is not None
    assert context["observed_input_tokens"] == 24_000
    assert context["calibration_factor"] == 2.4
    assert context["calibration_samples"] == 1
    assert context["calibration_version"] == 2


def test_compaction_updates_live_estimated_context(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "sessions")
    session_id = uuid4()
    run_id = uuid4()
    store.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="messages.snapshot",
            payload={"estimated_tokens": 60_000},
        )
    )
    store.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="context.compacted",
            payload={"estimated_tokens_before": 60_000, "estimated_tokens_after": 24_000},
        )
    )

    context = store.projection.context(session_id)

    assert context is not None
    assert context["estimated_history_tokens"] == 24_000
    assert context["compaction_count"] == 1


def test_noop_compaction_does_not_increment_the_projection(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "sessions")
    session_id = uuid4()
    store.append(
        Event(
            session_id=session_id,
            run_id=uuid4(),
            agent_id="main",
            type="context.compaction_noop",
            payload={"estimated_tokens_before": 60_000, "estimated_tokens_after": 60_000},
        )
    )

    context = store.projection.context(session_id)

    assert context is None or context["compaction_count"] == 0


def test_projection_can_correct_a_large_token_overestimate(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "sessions")
    session_id = uuid4()
    store.append(
        Event(
            session_id=session_id,
            run_id=uuid4(),
            agent_id="main",
            type="messages.snapshot",
            payload={
                "estimated_tokens": 167_386,
                "estimated_latest_request_tokens": 148_783,
                "latest_request_input_tokens": 40_471,
            },
        )
    )

    context = store.projection.context(session_id)

    assert context is not None
    assert context["calibration_factor"] == pytest.approx(40_471 / 148_783)
