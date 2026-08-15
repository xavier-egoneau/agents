from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_kernel.models import RunError, RunRequest, RunResult, RunStatus, SecurityMode
from agentic_kernel.scheduler import (
    LEGACY_ROUTINE_INBOX,
    CronJobInput,
    CronScheduler,
    CronService,
    SchedulerError,
    agent_session_id,
)


def _operations_function(name: str):
    """Charge le module operations comme le fait le registre, sans exiger
    `tools` sur `sys.path` — le lancement direct de pytest ne l'ajoute pas."""
    path = Path(__file__).parents[1] / "tools" / "modules" / "operations" / "module.py"
    spec = importlib.util.spec_from_file_location("amk_operations_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


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


def workflow_definition(tool: str | None = "utc_now") -> dict:
    steps: list[dict] = []
    if tool:
        steps.append({"id": "source", "kind": "tool", "tool": tool, "args": {}})
    steps.append(
        {
            "id": "summary",
            "kind": "synthesize",
            "needs": ["source"] if tool else [],
            "instructions": "Résumer les résultats disponibles.",
        }
    )
    return {
        "schema": "amk.workflow/v1",
        "id": "routine-test-v1",
        "title": "Routine test",
        "status": "ready",
        "execution": {
            "mode": "agent_guided",
            "deviation": "stop_and_report",
            "timezone": "Europe/Paris",
        },
        "permissions": {
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": tool}] if tool else [],
        },
        "missing_dependencies": [],
        "steps": steps,
        "output": {"sections": ["Résultat"]},
    }


def test_cron_crud_and_schedule_validation(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    assert job.workspace == workspace.resolve()
    assert job.notification_session_id == agent_session_id("main")
    assert job.next_run_at is not None
    assert service.list()[0].id == job.id

    updated = service.update(job.id, payload(workspace, enabled=False))
    assert not updated.enabled
    service.delete(job.id)
    assert service.list() == []
    with pytest.raises(SchedulerError):
        service.create(payload(workspace, schedule="not a cron"))


def test_typed_errors_carry_their_http_meaning(tmp_path: Path) -> None:
    """« Introuvable », « pendant une exécution » : la distinction vit dans le
    type, pas dans le libellé — le router la portait par sous-chaînes."""
    from agentic_kernel.scheduler import CronConflictError, CronNotFoundError

    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))

    with pytest.raises(CronNotFoundError):
        service.get("cron_absent")
    with pytest.raises(CronNotFoundError):
        service.delete("cron_absent")

    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute("UPDATE cron_jobs SET in_flight=1 WHERE id=?", (job.id,))
    with pytest.raises(CronConflictError):
        service.update_with_workflow(job.id, payload(workspace), workflow_definition(), "hash")


def test_disappeared_workspace_still_allows_updating_the_routine(tmp_path: Path) -> None:
    """Une routine dont le dossier a disparu doit rester désactivable.

    L'interface renvoie le workspace enregistré tel quel à chaque mise à jour :
    revalider un champ inchangé rendait la routine impossible à désactiver ou à
    corriger dès que son dossier était déplacé ou hérité d'une autre machine.
    """
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))

    workspace.rmdir()

    updated = service.update(job.id, payload(workspace, enabled=False))
    assert not updated.enabled
    assert updated.workspace == workspace.resolve()


def test_workspace_from_another_operating_system_is_left_untouched(tmp_path: Path) -> None:
    """Une base déplacée entre systèmes contient des chemins d'un autre OS.

    Résoudre un tel chemin fabrique une valeur différente de celle enregistrée
    — sous Windows, `resolve()` préfixe un chemin POSIX absolu par la lettre du
    lecteur courant. Comparer après résolution faisait passer un champ inchangé
    pour une modification, et bloquait toute mise à jour de la routine.
    """
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    foreign = Path("/Users/quelquun/projets/agents")
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute(
            "UPDATE cron_jobs SET workspace = ? WHERE id = ?", (str(foreign), job.id)
        )

    reloaded = service.get(job.id)
    updated = service.update(job.id, payload(reloaded.workspace, enabled=False))

    assert not updated.enabled
    assert updated.workspace == reloaded.workspace


@pytest.mark.skipif(os.name != "nt", reason="normcase n'ignore la casse que sous Windows")
def test_workspace_comparison_ignores_case_on_windows(tmp_path: Path) -> None:
    """Le chemin relu depuis SQLite n'est pas passé par ``resolve()``.

    Sous Windows une simple différence de casse suffisait à le faire considérer
    comme modifié, donc à le revalider — et à rejeter la mise à jour d'une
    routine dont le dossier n'existe plus.
    """
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "Project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    workspace.rename(tmp_path / "Project-renamed")

    variant = Path(str(job.workspace).swapcase())
    updated = service.update(job.id, payload(variant, enabled=False))

    assert not updated.enabled


def test_moving_a_routine_to_a_missing_workspace_is_still_rejected(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))

    with pytest.raises(SchedulerError, match="Workspace introuvable"):
        service.update(job.id, payload(tmp_path / "ailleurs"))


def test_cron_can_run_without_an_associated_workspace(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")

    job = service.create(payload(tmp_path).model_copy(update={"workspace": None}))

    assert job.workspace is None
    assert CronScheduler.request_for(job).workspace is None


def test_next_run_stays_in_the_workflow_timezone_after_a_utc_claim(
    tmp_path: Path,
) -> None:
    service = CronService(tmp_path / "state.db")
    job = service.create(
        payload(tmp_path).model_copy(update={"workspace": None}),
        workflow=workflow_definition(),
        workflow_basis_hash="sha256:basis",
    )
    summer_morning_utc = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)

    claimed = service.claim(job.id, summer_morning_utc, scheduled_for=summer_morning_utc)

    assert claimed is not None
    following = service.get(job.id).next_run_at
    assert following is not None
    assert following.isoformat() == "2026-08-05T09:00:00+02:00"


def test_cron_workflow_is_optional_persistent_and_independently_removable(
    tmp_path: Path,
) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    workflow = workflow_definition()

    free_job = service.create(payload(workspace, name="Free", skills=["review"]))
    assert free_job.workflow is None
    assert free_job.workflow_revision == 0
    free_request = CronScheduler.request_for(free_job)
    assert free_request.workflow is None
    assert free_request.tool_allowlist is None

    guided = service.create(
        payload(workspace, name="Guided", skills=["review"]),
        workflow=workflow,
        workflow_basis_hash="sha256:basis",
    )
    assert guided.workflow == workflow
    assert guided.workflow_revision == 1
    assert guided.workflow_basis_hash == "sha256:basis"
    assert guided.workflow_updated_at is not None
    request = CronScheduler.request_for(guided)
    assert request.skills == ["review"]
    assert request.workflow == workflow
    assert request.tool_allowlist == ["utc_now"]

    updated = service.update(
        guided.id,
        payload(
            workspace,
            name="Renamed",
            prompt="Keep the accepted workflow",
            skills=["review"],
        ),
    )
    assert updated.workflow == workflow
    assert updated.workflow_revision == 1

    cleared = service.clear_workflow(guided.id)
    assert cleared.id == guided.id
    assert cleared.prompt == "Keep the accepted workflow"
    assert cleared.skills == ["review"]
    assert cleared.workflow is None
    assert cleared.workflow_revision == 2
    assert CronScheduler.request_for(cleared).tool_allowlist is None


def test_synthesis_only_workflow_explicitly_allows_no_tools(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(
        payload(workspace),
        workflow=workflow_definition(None),
        workflow_basis_hash="sha256:basis",
    )

    assert CronScheduler.request_for(job).tool_allowlist == []


def test_non_ready_workflow_exposes_no_tools_as_a_runtime_backstop(
    tmp_path: Path,
) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    workflow = workflow_definition()
    workflow["status"] = "blocked"
    workflow["missing_dependencies"] = [
        {"capability": "calendar.events.list", "reason": "Connecteur absent"}
    ]
    job = service.create(
        payload(workspace, enabled=False),
        workflow=workflow,
        workflow_basis_hash="sha256:basis",
    )

    assert CronScheduler.request_for(job).tool_allowlist == []


def test_workflow_cannot_change_while_job_is_in_flight(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = service.create(payload(workspace))
    claimed = service.claim(job.id)
    assert claimed is not None

    with pytest.raises(SchedulerError, match="pendant une exécution"):
        service.set_workflow(
            job.id,
            workflow_definition(),
            "sha256:basis",
        )
    with pytest.raises(SchedulerError, match="pendant une exécution"):
        service.clear_workflow(job.id)


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
    assert repaired.get(job.id).notification_session_id == agent_session_id("main")
    assert repaired.list_runs()[0].notification_session_id == agent_session_id("main")


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


def test_a_routine_pointing_at_the_legacy_inbox_is_repointed(tmp_path: Path) -> None:
    """La cible périmée ne déclenchait aucun repli.

    L'ancienne boîte existait toujours en projection : `deliver_cron_result` la
    trouvait, ne basculait donc pas sur le canal de l'agent, et les résultats
    tombaient dans un fil qu'aucun écran ne montre plus. Un échec franc aurait
    été moins coûteux qu'une livraison silencieuse au mauvais endroit.
    """
    base = tmp_path / "state.db"
    service = CronService(base)
    job = service.create(
        CronJobInput(name="Veille", schedule="0 8 * * *", prompt="veille", agent_id="main")
    )
    with sqlite3.connect(base) as connection:
        connection.execute(
            "UPDATE cron_jobs SET notification_session_id=? WHERE id=?",
            (str(LEGACY_ROUTINE_INBOX), job.id),
        )

    repris = CronService(base).get(job.id)

    assert repris.notification_session_id == agent_session_id("main")


def test_one_shot_requires_schedule_or_one_shot_at() -> None:
    """Un cron ponctuel doit avoir la date ET le schedule vide."""
    with pytest.raises(ValueError, match="schedule cron"):
        CronJobInput(name="Sans date ni schedule", prompt="test", schedule="")
    CronJobInput(
        name="Avec date",
        prompt="test",
        schedule="",
        one_shot_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
    )


def test_one_shot_next_fire_is_the_date_itself(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    one_shot = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    job = service.create(
        CronJobInput(
            name="One-shot précis",
            schedule="",
            prompt="Faire le déploiement",
            one_shot_at=one_shot,
        )
    )
    assert job.next_run_at == one_shot
    assert job.schedule == ""


def test_one_shot_is_due_at_the_exact_time(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    one_shot = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    service.create(
        CronJobInput(
            name="One-shot à l'heure",
            schedule="",
            prompt="Déployer",
            one_shot_at=one_shot,
        )
    )
    before = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    assert service.due(before) == []
    at = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    assert len(service.due(at)) == 1
    after = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    assert len(service.due(after)) == 1


def test_one_shot_is_claimed_once_then_never_again(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    one_shot = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    job = service.create(
        CronJobInput(
            name="One-shot unique",
            schedule="",
            prompt="Exécuter une seule fois",
            one_shot_at=one_shot,
        )
    )
    claimed = service.claim(job.id, now=one_shot)
    assert claimed is not None
    fresh = service.get(job.id)
    assert fresh.next_run_at is None
    assert fresh.in_flight
    # Finir le run
    service.finish(job.id, None)
    final = service.get(job.id)
    assert not final.in_flight
    assert final.next_run_at is None
    # Après execution, `due` ne le retourne plus
    later = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    assert service.due(later) == []


def test_regular_cron_is_not_affected_by_one_shot_logic(tmp_path: Path) -> None:
    service = CronService(tmp_path / "state.db")
    job = service.create(
        CronJobInput(name="Régulière", schedule="*/5 * * * *", prompt="Toutes les 5 min")
    )
    claimed = service.claim(job.id, now=datetime(2026, 8, 15, 9, 0, tzinfo=UTC))
    assert claimed is not None
    fresh = service.get(job.id)
    assert fresh.next_run_at is not None
    # La prochaine échéance doit être après la date de claim
    assert fresh.next_run_at > datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def test_a_reminder_fires_once_and_never_again(tmp_path: Path) -> None:
    """Un rappel n'est pas une routine annuelle.

    `cron_create` n'acceptait qu'un `schedule` cron : demander « rappelle-moi le
    11 août » ne pouvait produire que `0 7 11 8 *`, qui revient chaque année.
    Ce n'était pas une erreur de compréhension de l'agent, c'était un paramètre
    absent de l'outil.
    """
    service = CronService(tmp_path / "state.db")

    job = service.create(
        CronJobInput(
            name="rappel",
            prompt="Prépare le planning",
            one_shot_at=datetime(2026, 8, 11, 7, 0, tzinfo=UTC),
        )
    )

    assert job.schedule == ""
    assert job.one_shot_at is not None


def test_a_routine_still_requires_one_of_the_two(tmp_path: Path) -> None:
    """Ni l'un ni l'autre laisserait une routine qui ne part jamais."""
    with pytest.raises(ValidationError):
        CronJobInput(name="vide", prompt="rien")


def test_a_prompt_that_ships_the_message_itself_is_refused() -> None:
    """Une description se laisse ignorer; un refus, non.

    Trois routines de suite ont été créées avec un prompt qui refabriquait la
    livraison — appel à l'API Telegram, jeton nommé en clair — alors qu'elle est
    automatique. Chaque fois, l'utilisateur devait aller accorder une
    autorisation réseau pour un rappel de deux lignes.
    """
    _refabrique_la_livraison = _operations_function("_refabrique_la_livraison")

    assert _refabrique_la_livraison(
        "Envoie un message Telegram : Médecin ! Utilise http_request avec "
        "TELEGRAM_MAIN_BOT_TOKEN et TELEGRAM_MAIN_USER_ID."
    )
    assert _refabrique_la_livraison("POST https://api.telegram.org/bot123/sendMessage")


def test_a_legitimate_outbound_call_stays_allowed() -> None:
    """On refuse l'auto-livraison, pas l'idée d'appeler une API."""
    _refabrique_la_livraison = _operations_function("_refabrique_la_livraison")

    assert not _refabrique_la_livraison("Rendez-vous médecin à 13h40")
    assert not _refabrique_la_livraison(
        "Publie le rapport sur le webhook interne https://hooks.exemple.test/rapport"
    )


def _resultat(statut: RunStatus, *, retryable: bool = False) -> RunResult:
    return RunResult(
        session_id=uuid4(),
        run_id=uuid4(),
        agent_id="main",
        status=statut,
        output="Rendez-vous médecin à 13h40",
        errors=[RunError(type="tool", message="échec", retryable=retryable)]
        if statut is not RunStatus.SUCCESS
        else [],
    )


def test_a_delivered_reminder_disappears(tmp_path: Path) -> None:
    """Un rappel est ponctuel : passée sa date, il n'a plus rien à faire.

    Le laisser en place produisait une routine active sans exécution prévue,
    qui s'accumulait à chaque rappel demandé. Le supprimer ne perd rien — le
    message vit dans la session canonique de l'agent, là où on le lit.
    """
    service = CronService(tmp_path / "state.db")
    job = service.create(
        CronJobInput(
            name="rappel",
            prompt="Rendez-vous médecin à 13h40",
            one_shot_at=datetime.now(UTC) + timedelta(minutes=1),
            kind="reminder",
        )
    )

    service.finish(job.id, _resultat(RunStatus.SUCCESS))

    assert [item.id for item in service.list()] == []


def test_a_failed_reminder_is_kept(tmp_path: Path) -> None:
    """Effacer un rappel qui n'est jamais arrivé priverait l'utilisateur de la
    seule trace lui disant qu'il l'attend encore."""
    service = CronService(tmp_path / "state.db")
    job = service.create(
        CronJobInput(
            name="rappel",
            prompt="Rendez-vous",
            one_shot_at=datetime.now(UTC) + timedelta(minutes=1),
            kind="reminder",
        )
    )

    service.finish(job.id, _resultat(RunStatus.FAILED))

    assert [item.id for item in service.list()] == [job.id]


def test_a_recurring_routine_survives_its_run(tmp_path: Path) -> None:
    """La suppression vise le ponctuel; une veille quotidienne doit rester."""
    service = CronService(tmp_path / "state.db")
    job = service.create(
        CronJobInput(name="veille", prompt="Cherche", schedule="0 8 * * *")
    )

    service.finish(job.id, _resultat(RunStatus.SUCCESS))

    assert [item.id for item in service.list()] == [job.id]
