from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from .models import ApprovalRequest


class ApprovalStore:
    def __init__(self, sessions_root: Path) -> None:
        self.root = sessions_root / "pending"

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

    def remove(self, approval_id: UUID) -> None:
        self.path_for(approval_id).unlink(missing_ok=True)

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
