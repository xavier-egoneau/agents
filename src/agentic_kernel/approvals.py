from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

from .models import ApprovalRequest


class ApprovalStore:
    def __init__(self, sessions_root: Path) -> None:
        self.root = sessions_root / "pending"
        self._lock = Lock()

    def save_state(self, approval: ApprovalRequest, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(approval.approval_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"approval": approval.model_dump(mode="json"), **state}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, target)

    def load_state(self, approval_id: UUID) -> dict[str, Any] | None:
        path = self.path_for(approval_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_pending(self) -> list[ApprovalRequest]:
        if not self.root.exists():
            return []
        requests = []
        for path in sorted(self.root.glob("*.json")):
            try:
                state = json.loads(path.read_text())
                if "decision" in state:
                    continue
                requests.append(ApprovalRequest.model_validate(state["approval"]))
            except (OSError, ValueError, KeyError):
                continue
        return requests

    def states_for_run(self, session_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        states: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                approval = ApprovalRequest.model_validate(state["approval"])
            except (OSError, ValueError, KeyError):
                continue
            if approval.session_id == session_id and approval.run_id == run_id:
                states.append(state)
        return states

    def path_for(self, approval_id: UUID) -> Path:
        return self.root / f"{approval_id}.json"

    def remove(self, approval_id: UUID) -> None:
        self.path_for(approval_id).unlink(missing_ok=True)

    def remove_for_session(self, session_id: UUID) -> None:
        if not self.root.exists():
            return
        for path in self.root.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                approval = ApprovalRequest.model_validate(state["approval"])
            except (OSError, ValueError, KeyError):
                continue
            if approval.session_id == session_id:
                path.unlink(missing_ok=True)

    def resolve_many(self, approval_ids: list[UUID], approved: bool) -> list[dict[str, Any]]:
        """Persist one batch as a single guarded filesystem transaction."""
        with self._lock:
            states: list[dict[str, Any]] = []
            targets: list[Path] = []
            for approval_id in approval_ids:
                target = self.path_for(approval_id)
                if not target.exists():
                    raise ValueError(f"unknown pending approval: {approval_id}")
                state = json.loads(target.read_text(encoding="utf-8"))
                if "decision" in state:
                    raise ValueError(f"approval already resolved: {approval_id}")
                states.append(state)
                targets.append(target)
            temporary: list[Path] = []
            try:
                for target, state in zip(targets, states, strict=True):
                    candidate = target.with_suffix(".batch.tmp")
                    candidate.write_text(
                        json.dumps(
                            {**state, "decision": approved},
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    candidate.chmod(0o600)
                    temporary.append(candidate)
                for candidate, target in zip(temporary, targets, strict=True):
                    os.replace(candidate, target)
            finally:
                for candidate in temporary:
                    candidate.unlink(missing_ok=True)
            return [{**state, "decision": approved} for state in states]
