from pathlib import Path
from uuid import uuid4

from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import Event


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
