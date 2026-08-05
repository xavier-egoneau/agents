from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from .events import JsonlEventStore
from .models import BudgetConfig, Event, RunResult, SecurityMode
from .orchestration import RuntimeDeps
from .secrets import SecretStore
from .snapshots import SnapshotStore


class RunExecutor:
    """Shared durable lifecycle and runtime-dependency construction for every trigger."""

    def __init__(
        self,
        events: JsonlEventStore,
        secrets: SecretStore,
        snapshots: SnapshotStore,
        state_db: Path,
    ) -> None:
        self.events = events
        self.secrets = secrets
        self.snapshots = snapshots
        self.state_db = state_db

    def dependencies(
        self,
        *,
        session_id: UUID,
        run_id: UUID,
        budgets: BudgetConfig,
        workspace: Path,
        security_mode: SecurityMode,
        approved_scopes: set[tuple[str, str, str | None]],
        tool_catalog: list[dict],
        provider_id: str,
        model_name: str | None,
        context_window_tokens: int | None,
        context_calibration: float = 1.0,
    ) -> RuntimeDeps:
        return RuntimeDeps(
            session_id=session_id,
            root_run_id=run_id,
            budgets=budgets,
            events=self.events,
            semaphore=asyncio.Semaphore(budgets.max_concurrency),
            workspace=workspace,
            security_mode=security_mode,
            approved_scopes=approved_scopes,
            tool_catalog=tool_catalog,
            state_db=self.state_db,
            provider_id=provider_id,
            model_name=model_name,
            context_window_tokens=context_window_tokens,
            context_calibration=context_calibration,
            secret_resolver=self.secrets.resolve,
            secret_redactor=self.secrets.redact,
            snapshot_store=self.snapshots,
        )

    def transition(
        self,
        *,
        session_id: UUID,
        run_id: UUID,
        agent_id: str,
        state: str,
        previous: str | None = None,
    ) -> None:
        payload = {"state": state}
        if previous:
            payload["from"] = previous
        self.events.append(
            Event(
                session_id=session_id,
                run_id=run_id,
                agent_id=agent_id,
                type="run.transitioned",
                payload=payload,
            )
        )

    def suspend(self, *, session_id: UUID, run_id: UUID, agent_id: str) -> None:
        self.events.append(
            Event(
                session_id=session_id,
                run_id=run_id,
                agent_id=agent_id,
                type="run.suspended",
                payload={"state": "approval_pending"},
            )
        )

    def terminal(self, result: RunResult) -> None:
        self.transition(
            session_id=result.session_id,
            run_id=result.run_id,
            agent_id=result.agent_id,
            state=result.status.value,
        )
        self.events.append(
            Event(
                session_id=result.session_id,
                run_id=result.run_id,
                agent_id=result.agent_id,
                type="session.completed",
                payload=result.model_dump(mode="json"),
            )
        )
