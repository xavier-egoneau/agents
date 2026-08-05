from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.approval_service import ApprovalResume, ApprovalService
from agentic_kernel.approvals import ApprovalStore
from agentic_kernel.errors import ConfigurationError
from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import ApprovalRequest, RunRequest, RunResult, RunStatus


def _approval(session_id, run_id, call_id: str) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        run_id=run_id,
        agent_id="main",
        tool_call_id=call_id,
        tool_name="write",
        action_family="write",
        path=f"/workspace/{call_id}.txt",
        justification="Test the durable batch.",
        reason="Overwrite requires approval.",
    )


def test_partial_batch_stays_suspended_then_resumes_once(tmp_path: Path) -> None:
    events = JsonlEventStore(tmp_path / "sessions")
    store = ApprovalStore(tmp_path / "sessions")
    service = ApprovalService(store, events)
    request = RunRequest(prompt="Update both")
    run_id = uuid4()
    first = _approval(request.session_id, run_id, "first")
    second = _approval(request.session_id, run_id, "second")
    state = {"request": request.model_dump(mode="json"), "messages": "[]"}
    store.save_state(first, state)
    store.save_state(second, state)

    waiting = service.resolve(first.approval_id, True)
    assert isinstance(waiting, RunResult)
    assert waiting.status is RunStatus.APPROVAL_PENDING

    ready = service.resolve(second.approval_id, False)
    assert isinstance(ready, ApprovalResume)
    assert set(ready.tool_results) == {"first", "second"}
    assert ready.approved_scopes == {("write", "write", "/workspace/first.txt")}
    assert service.approved_scopes(request.session_id) == {
        ("write", "write", "/workspace/first.txt")
    }
    assert store.states_for_run(request.session_id, run_id) == []
    assert [event.type for event in events.read(request.session_id)].count(
        "approval.resolved"
    ) == 2

    with pytest.raises(ConfigurationError, match="unknown pending approval"):
        service.resolve(second.approval_id, False)


def test_batch_rejects_approvals_from_different_runs(tmp_path: Path) -> None:
    events = JsonlEventStore(tmp_path / "sessions")
    store = ApprovalStore(tmp_path / "sessions")
    service = ApprovalService(store, events)
    request = RunRequest(prompt="Update")
    approvals = [
        _approval(request.session_id, uuid4(), "first"),
        _approval(request.session_id, uuid4(), "second"),
    ]
    state = {"request": request.model_dump(mode="json"), "messages": "[]"}
    for approval in approvals:
        store.save_state(approval, state)

    with pytest.raises(ConfigurationError, match="same run"):
        service.resolve_many([item.approval_id for item in approvals], True)


def test_store_rejects_stale_batch_atomically_when_a_newer_run_exists(
    tmp_path: Path,
) -> None:
    store = ApprovalStore(tmp_path / "sessions")
    request = RunRequest(prompt="Update")
    older_run = uuid4()
    created_at = datetime.now(UTC)
    older = [
        _approval(request.session_id, older_run, call_id).model_copy(
            update={"created_at": created_at}
        )
        for call_id in ("first", "second")
    ]
    newer = _approval(request.session_id, uuid4(), "newer").model_copy(
        update={"created_at": created_at + timedelta(seconds=1)}
    )
    state = {"request": request.model_dump(mode="json"), "messages": "[]"}
    for approval in [*older, newer]:
        store.save_state(approval, state)

    with pytest.raises(ValueError, match="newer approval batch"):
        store.resolve_many([item.approval_id for item in older], True)

    # The stale decision is all-or-nothing: no target is written before the
    # newer-run guard rejects the transaction.
    assert all("decision" not in store.load_state(item.approval_id) for item in older)
    assert "decision" not in store.load_state(newer.approval_id)
