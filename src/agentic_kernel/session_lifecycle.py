from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID

from .kernel import Kernel
from .models import Event
from .plans import PlanService


class SessionLifecycle:
    """Clear every durable component owned by a conversation."""

    def __init__(self, kernel: Kernel, state_database: Path) -> None:
        self.kernel = kernel
        self.plans = PlanService(state_database)

    def delete(self, session_id: UUID) -> bool:
        deleted = self.kernel.events.delete(session_id)
        self._clear_related_state(session_id)
        return deleted

    def reset(self, event: Event) -> None:
        self.kernel.events.reset(event)
        self._clear_related_state(event.session_id)

    def _clear_related_state(self, session_id: UUID) -> None:
        self.kernel.approvals.remove_for_session(session_id)
        self.kernel.snapshots.clear(session_id)
        artifact_root = (self.kernel.events.directory / "artifacts").resolve()
        artifact_directory = (artifact_root / str(session_id)).resolve()
        artifact_directory.relative_to(artifact_root)
        shutil.rmtree(artifact_directory, ignore_errors=True)
        self.plans.delete_for_session(session_id)
