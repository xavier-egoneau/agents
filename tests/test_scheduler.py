from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.models import RunError, RunRequest, RunResult, RunStatus, SecurityMode
from agentic_kernel.scheduler import (
    ROUTINE_INBOX_SESSION_ID,
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
    assert job.notification_session_id == ROUTINE_INBOX_SESSION_ID
    assert job.next_run_at is not None
    assert service.list()[0].id == job.id

    updated = service.update(job.id, payload(workspace, enabled=False))
    assert not updated.enabled
    service.delete(job.id)
    assert service.list() == []
    with pytest.raises(SchedulerError):
        service.create(payload(workspace, schedule="not a cron"))


def test_cron_can_deliver_to_a_selected_session(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    destination = uuid4()
    job = service.create(payload(workspace, notification_session_id=destination))
    claimed = service.claim(job.id)
    assert claimed is not None
    occurrence = service.list_runs()[0]
    assert occurrence.session_id == job.session_id
    assert occurrence.notification_session_id == destination


def test_existing_nullable_notification_columns_are_always_backfilled(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = CronService(database)
    job = service.create(payload(workspace))
    claimed = service.claim(job.id)
    assert claimed is not None

    with service._connect() as connection:
        connection.execute(
            "UPDATE cron_jobs SET notification_session_id=NULL WHERE id=?",
            (job.id,),
        )
        connection.execute(
            "UPDATE cron_runs SET notification_session_id=NULL WHERE cron_job_id=?",
            (job.id,),
        )

    repaired = CronService(database)
    assert repaired.get(job.id).notification_session_id == ROUTINE_INBOX_SESSION_ID
    assert repaired.list_runs()[0].notification_session_id == ROUTINE_INBOX_SESSION_ID


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


def test_successful_test_clears_an_obsolete_cron_error(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    next_run_at = job.next_run_at
    failed = RunResult(
        session_id=job.session_id,
        run_id=uuid4(),
        agent_id="main",
        status=RunStatus.FAILED,
        errors=[RunError(type="module", message="stale index")],
    )
    service.finish(job.id, failed)
    assert service.get(job.id).last_error == "stale index"

    succeeded = RunResult(
        session_id=job.session_id,
        run_id=uuid4(),
        agent_id="main",
        status=RunStatus.SUCCESS,
    )
    service.record_test_result(job.id, succeeded)

    refreshed = service.get(job.id)
    assert refreshed.last_status == "success"
    assert refreshed.last_error is None
    assert refreshed.next_run_at == next_run_at


def test_pending_test_does_not_block_the_schedule(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    pending = RunResult(
        session_id=job.session_id,
        run_id=uuid4(),
        agent_id="main",
        status=RunStatus.APPROVAL_PENDING,
    )

    service.record_test_result(job.id, pending)

    refreshed = service.get(job.id)
    assert refreshed.last_status == "approval_pending"
    assert not refreshed.blocked


def test_legacy_test_only_block_is_repaired_on_startup(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = CronService(database)
    job = service.create(payload(workspace))
    with service._connect() as connection:
        connection.execute("UPDATE cron_jobs SET blocked=1 WHERE id=?", (job.id,))

    repaired = CronService(database)

    assert not repaired.get(job.id).blocked


def test_occurrences_are_durable_idempotent_and_deliverable(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    scheduled_for = datetime.now(UTC).replace(microsecond=0)

    claimed = service.claim(job.id, scheduled_for, scheduled_for=scheduled_for)
    assert claimed is not None
    assert claimed.occurrence_id
    service.mark_started(claimed.occurrence_id)
    result = RunResult(
        session_id=job.session_id,
        run_id=uuid4(),
        agent_id="main",
        status=RunStatus.SUCCESS,
        output="Résultat livré",
    )
    service.finish(job.id, result, occurrence_id=claimed.occurrence_id)

    occurrence = service.list_runs()[0]
    assert occurrence.execution_status == "success"
    assert occurrence.output_preview == "Résultat livré"
    assert occurrence.delivery_status == "unread"
    assert service.mark_run_read(occurrence.id).delivery_status == "read"

    # The same scheduled occurrence cannot be claimed twice.
    assert service.claim(job.id, scheduled_for, scheduled_for=scheduled_for) is None


def test_approval_pending_blocks_future_occurrences_until_resume(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    scheduled_for = datetime.now(UTC)
    claimed = service.claim(job.id, scheduled_for, scheduled_for=scheduled_for)
    assert claimed and claimed.occurrence_id
    pending = RunResult(
        session_id=job.session_id,
        run_id=uuid4(),
        agent_id="main",
        status=RunStatus.APPROVAL_PENDING,
    )
    service.finish(job.id, pending, occurrence_id=claimed.occurrence_id)

    assert service.get(job.id).blocked
    assert service.due(scheduled_for + timedelta(days=1)) == []
    assert service.claim(job.id, scheduled_for + timedelta(minutes=1)) is None

    resumed = pending.model_copy(
        update={"status": RunStatus.SUCCESS, "output": "Après autorisation"}
    )
    service.resume_for_session(job.session_id, resumed)
    assert not service.get(job.id).blocked
    assert service.list_runs()[0].execution_status == "success"


def test_resume_occurrence_finishes_only_the_linked_occurrence_with_a_child_run(
    tmp_path: Path,
) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    first_job = service.create(payload(workspace, name="First"))
    second_job = service.create(payload(workspace, name="Second"))

    # Put both routines on the same durable execution session. This makes the
    # assertion stronger than merely separating them by session id.
    with service._connect() as connection:
        connection.execute(
            "UPDATE cron_jobs SET session_id=? WHERE id=?",
            (str(first_job.session_id), second_job.id),
        )
    second_job = service.get(second_job.id)

    now = datetime.now(UTC)
    first_claim = service.claim(first_job.id, now, scheduled_for=now)
    second_claim = service.claim(
        second_job.id,
        now + timedelta(minutes=1),
        scheduled_for=now + timedelta(minutes=1),
    )
    assert first_claim and first_claim.occurrence_id
    assert second_claim and second_claim.occurrence_id
    first_root_run = uuid4()
    second_root_run = uuid4()
    service.finish(
        first_job.id,
        RunResult(
            session_id=first_job.session_id,
            run_id=first_root_run,
            agent_id="main",
            status=RunStatus.APPROVAL_PENDING,
        ),
        occurrence_id=first_claim.occurrence_id,
    )
    service.finish(
        second_job.id,
        RunResult(
            session_id=first_job.session_id,
            run_id=second_root_run,
            agent_id="main",
            status=RunStatus.APPROVAL_PENDING,
        ),
        occurrence_id=second_claim.occurrence_id,
    )

    child_run = uuid4()
    service.resume_occurrence(
        first_claim.occurrence_id,
        RunResult(
            session_id=first_job.session_id,
            run_id=child_run,
            agent_id="main",
            status=RunStatus.SUCCESS,
            output="Approved child run",
        ),
    )

    assert not service.get(first_job.id).blocked
    assert service.get(second_job.id).blocked
    occurrences = {item.id: item for item in service.list_runs(limit=10)}
    assert occurrences[first_claim.occurrence_id].execution_status == "success"
    assert occurrences[first_claim.occurrence_id].run_id == child_run
    assert occurrences[second_claim.occurrence_id].execution_status == "blocked"
    assert occurrences[second_claim.occurrence_id].run_id == second_root_run


def test_repair_orphaned_blocks_preserves_sessions_with_pending_approvals(
    tmp_path: Path,
) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    orphan = service.create(payload(workspace, name="Orphan"))
    pending = service.create(payload(workspace, name="Still pending"))
    now = datetime.now(UTC)
    orphan_claim = service.claim(orphan.id, now, scheduled_for=now)
    pending_claim = service.claim(
        pending.id,
        now + timedelta(minutes=1),
        scheduled_for=now + timedelta(minutes=1),
    )
    assert orphan_claim and orphan_claim.occurrence_id
    assert pending_claim and pending_claim.occurrence_id
    for job, claimed in ((orphan, orphan_claim), (pending, pending_claim)):
        service.finish(
            job.id,
            RunResult(
                session_id=job.session_id,
                run_id=uuid4(),
                agent_id="main",
                status=RunStatus.APPROVAL_PENDING,
            ),
            occurrence_id=claimed.occurrence_id,
        )

    repaired = service.repair_orphaned_blocks({pending.session_id})

    assert repaired == 1
    assert not service.get(orphan.id).blocked
    assert service.get(pending.id).blocked
    occurrences = {item.id: item for item in service.list_runs(limit=10)}
    repaired_occurrence = occurrences[orphan_claim.occurrence_id]
    assert repaired_occurrence.execution_status == "cancelled"
    assert repaired_occurrence.task_status == "cancelled"
    assert repaired_occurrence.completed_at is not None
    assert occurrences[pending_claim.occurrence_id].execution_status == "blocked"


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
