from pathlib import Path
from uuid import uuid4

from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import Event


def test_jsonl_is_append_only_and_replayable(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    session_id = uuid4()
    root = uuid4()
    child = uuid4()
    store.append(
        Event(session_id=session_id, run_id=root, agent_id="main", type="started")
    )
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
