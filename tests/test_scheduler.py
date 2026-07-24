from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.models import RunError, RunRequest, RunResult, RunStatus, SecurityMode
from agentic_kernel.scheduler import (
    CronJobInput,
    CronScheduler,
    CronService,
    SchedulerError,
)


def payload(workspace: Path, **updates) -> CronJobInput:
    values = {
        "name": "Daily review",
        "schedule": "0 9 * * *",
        "prompt": "Review the project",
        "workspace": workspace,
        "security_mode": SecurityMode.LIMITED,
    }
    values.update(updates)
    return CronJobInput(**values)


def test_cron_crud_and_schedule_validation(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    assert job.workspace == workspace.resolve()
    assert job.next_run_at is not None
    assert service.list()[0].id == job.id

    updated = service.update(job.id, payload(workspace, enabled=False))
    assert not updated.enabled
    service.delete(job.id)
    assert service.list() == []
    with pytest.raises(SchedulerError):
        service.create(payload(workspace, schedule="not a cron"))


def test_legacy_crons_are_imported_only_once(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = tmp_path / "crons.json"
    source.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "Legacy",
                        "schedule": "0 8 * * *",
                        "message": "Hello",
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert service.import_legacy_once(source, workspace) == 1
    assert service.import_legacy_once(source, workspace) == 0
    assert [job.name for job in service.list()] == ["Legacy"]


def test_cron_test_reuses_durable_session_without_mutating_schedule(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace, enabled=False))
    next_run_at = job.next_run_at

    request = CronScheduler.request_for(job, trigger="cron_test")

    assert request.trigger == "cron_test"
    assert request.session_id == job.session_id
    assert request.security_mode == SecurityMode.LIMITED
    assert service.get(job.id).next_run_at == next_run_at


@pytest.mark.asyncio
async def test_scheduler_uses_one_session_and_resumes_failed_job(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace, schedule="* * * * *"))
    calls: list[RunRequest] = []

    async def launch(request: RunRequest) -> RunResult:
        calls.append(request)
        return RunResult(
            session_id=request.session_id,
            run_id=uuid4(),
            agent_id=request.agent_id,
            status=RunStatus.FAILED if len(calls) == 1 else RunStatus.SUCCESS,
            errors=[RunError(type="provider", message="temporary", retryable=True)]
            if len(calls) == 1
            else [],
        )

    scheduler = CronScheduler(service, launch)
    first = service.claim(job.id, datetime.now(UTC))
    assert first is not None
    await scheduler._execute(first)
    second = service.claim(job.id, datetime.now(UTC) + timedelta(minutes=1))
    assert second is not None
    await scheduler._execute(second)

    assert len(calls) == 2
    assert calls[0].session_id == calls[1].session_id == job.session_id
    assert calls[0].trigger == "cron"
    assert calls[1].trigger == "cron_resume"
    assert "Ne rejoue pas" in calls[1].prompt
