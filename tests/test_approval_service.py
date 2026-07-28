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
    assert ready.approved_scopes == {("write", "/workspace/first.txt")}
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
