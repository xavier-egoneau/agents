from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic_ai.tools import ToolApproved, ToolDenied

from .errors import ConfigurationError
from .models import (
    ApprovalRequest,
    ApprovalResolution,
    Event,
    RunRequest,
    RunResult,
    RunStatus,
)


@dataclass(frozen=True)
class ApprovalResume:
    approval: ApprovalRequest
    request: RunRequest
    state: dict[str, Any]
    batch: list[dict[str, Any]]
    tool_results: dict[str, ToolApproved | ToolDenied]
    approved_scopes: set[tuple[str, str, str | None]]


class ApprovalService:
    """Durable approval decisions and preparation of one idempotent resume."""

    def __init__(self, store, events) -> None:
        self.store = store
        self.events = events

    def list_pending(self) -> list[ApprovalRequest]:
        return self.store.list_pending()

    def approved_scopes(self, session_id) -> set[tuple[str, str, str | None]]:
        scopes: set[tuple[str, str, str | None]] = set()
        events = self.events.read(session_id)
        requested_tools = {
            str(event.payload.get("approval_id")): event.payload.get("tool_name")
            for event in events
            if event.type == "approval.requested" and event.payload.get("approval_id")
        }
        for event in events:
            if event.type == "approval.grants.revoked":
                scopes.clear()
                continue
            if event.type == "approval.resolved" and event.payload.get("approved"):
                tool_name = event.payload.get("tool_name") or requested_tools.get(
                    str(event.payload.get("approval_id"))
                )
                # Very old journals without the matching request cannot be
                # interpreted as an exact grant and are deliberately ignored.
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                scopes.add(
                    (
                        tool_name,
                        event.payload.get("action_family", "other"),
                        event.payload.get("path"),
                    )
                )
        return scopes

    def persist_pending(self, request, run_id, deps, result) -> RunResult:
        for call in result.output.approvals:
            approval = deps.pending_approvals.get(call.tool_call_id)
            if approval is None:
                continue
            self.store.save_state(
                approval,
                {
                    "request": request.model_dump(mode="json"),
                    "messages": result.all_messages_json().decode(),
                },
            )
        return RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=RunStatus.APPROVAL_PENDING,
            output="Approval required before the run can continue.",
        )

    def resolve(
        self,
        approval_id,
        approved: bool,
        *,
        pre_resolved: bool = False,
    ) -> ApprovalResume | RunResult:
        approval_uuid = (
            approval_id if isinstance(approval_id, UUID) else UUID(str(approval_id))
        )
        state = self.store.load_state(approval_uuid)
        if state is None:
            raise ConfigurationError(f"unknown pending approval: {approval_uuid}")
        approval = ApprovalRequest.model_validate(state["approval"])
        self._ensure_latest_run(approval)
        if "decision" in state and not pre_resolved:
            raise ConfigurationError(f"approval already resolved: {approval_uuid}")
        request = RunRequest.model_validate(state["request"])
        if not pre_resolved:
            self._record_resolution(approval, approved)
            self.store.save_state(
                approval,
                {key: value for key, value in state.items() if key != "approval"}
                | {"decision": approved},
            )
        batch = self.store.states_for_run(request.session_id, approval.run_id)
        unresolved = [item for item in batch if "decision" not in item]
        if unresolved:
            return RunResult(
                session_id=request.session_id,
                run_id=approval.run_id,
                agent_id=request.agent_id,
                status=RunStatus.APPROVAL_PENDING,
                output=(
                    f"Approval recorded. {len(unresolved)} approval(s) still pending "
                    "before the run can continue."
                ),
            )
        self._supersede_older(approval)
        tool_results: dict[str, ToolApproved | ToolDenied] = {}
        scopes: set[tuple[str, str, str | None]] = set()
        for item in batch:
            batch_approval = ApprovalRequest.model_validate(item["approval"])
            if bool(item["decision"]):
                scopes.add(
                    (
                        batch_approval.tool_name,
                        batch_approval.action_family,
                        batch_approval.path,
                    )
                )
                tool_results[batch_approval.tool_call_id] = ToolApproved()
            else:
                tool_results[batch_approval.tool_call_id] = ToolDenied(
                    "Denied by the user."
                )
            self.store.remove(batch_approval.approval_id)
        return ApprovalResume(
            approval=approval,
            request=request,
            state=state,
            batch=batch,
            tool_results=tool_results,
            approved_scopes=scopes,
        )

    def resolve_many(
        self,
        approval_ids: list,
        approved: bool,
    ) -> ApprovalResume | RunResult:
        if not approval_ids:
            raise ConfigurationError("approval batch cannot be empty")
        ids = [item if isinstance(item, UUID) else UUID(str(item)) for item in approval_ids]
        pending_states = [self.store.load_state(item) for item in ids]
        if any(state is None for state in pending_states):
            raise ConfigurationError("approval batch contains an unknown approval")
        pending = [
            ApprovalRequest.model_validate(state["approval"])
            for state in pending_states
            if state is not None
        ]
        if len({(item.session_id, item.run_id) for item in pending}) != 1:
            raise ConfigurationError("all approvals in a batch must belong to the same run")
        self._ensure_latest_run(pending[-1])
        try:
            states = self.store.resolve_many(ids, approved)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigurationError(str(exc)) from exc
        approvals = [ApprovalRequest.model_validate(item["approval"]) for item in states]
        for approval in approvals:
            self._record_resolution(approval, approved)
        return self.resolve(approvals[-1].approval_id, approved, pre_resolved=True)

    def _ensure_latest_run(self, current: ApprovalRequest) -> None:
        if any(
            candidate.session_id == current.session_id
            and candidate.run_id != current.run_id
            and candidate.created_at > current.created_at
            for candidate in self.store.list_pending()
        ):
            raise ConfigurationError(
                "a newer approval batch exists for this session; reload before deciding"
            )

    def _record_resolution(
        self,
        approval: ApprovalRequest,
        approved: bool,
    ) -> None:
        resolution = ApprovalResolution(
            approval_id=approval.approval_id,
            approved=approved,
        )
        self.events.append(
            Event(
                session_id=approval.session_id,
                run_id=approval.run_id,
                agent_id=approval.agent_id,
                type="approval.resolved",
                payload={
                    **resolution.model_dump(mode="json"),
                    "action_family": approval.action_family,
                    "tool_name": approval.tool_name,
                    "path": approval.path,
                },
            )
        )

    def _supersede_older(self, current: ApprovalRequest) -> None:
        for stale in self.store.list_pending():
            if stale.session_id != current.session_id or stale.run_id == current.run_id:
                continue
            if stale.created_at >= current.created_at:
                continue
            self.events.append(
                Event(
                    session_id=current.session_id,
                    run_id=stale.run_id,
                    agent_id=stale.agent_id,
                    type="approval.superseded",
                    payload={
                        "approval_id": str(stale.approval_id),
                        "superseded_by_run_id": str(current.run_id),
                    },
                )
            )
            self.store.remove(stale.approval_id)
