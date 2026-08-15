from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..approval_service import CronTestApprovalBatch
from ..errors import KernelError
from ..guardian import guardian_parameters_schema
from ..kernel import Kernel
from ..models import Event, RunResult
from ..scheduler import (
    CronConflictError,
    CronJob,
    CronJobInput,
    CronNotFoundError,
    CronScheduler,
    CronService,
    SchedulerError,
)
from ..skills import render_skill
from ..workflows import (
    AcceptedWorkflow,
    StaleWorkflowError,
    WorkflowBasis,
    WorkflowDefinition,
    WorkflowGenerationError,
    WorkflowProposalService,
    WorkflowTool,
    WorkflowValidationError,
    validate_accepted,
)
from .runs import LaunchRun

_log = logging.getLogger(__name__)


class CronJobBody(CronJobInput):
    accepted_workflow: AcceptedWorkflow | None = None


class CronCreateBody(CronJobInput):
    accepted_workflow: AcceptedWorkflow | None = None


class CronWorkflowProposalBody(CronJobInput):
    timezone: str = "Europe/Paris"


class CronWorkflowAcceptanceBody(BaseModel):
    accepted_workflow: AcceptedWorkflow


WorkflowProposalFactory = Callable[[CronJobInput], WorkflowProposalService]


def create_cron_router(  # noqa: C901 - dette: factory à plusieurs endpoints
    service: CronService,
    scheduler: CronScheduler,
    launch: LaunchRun,
    kernel: Kernel,
    workflow_proposal_factory: WorkflowProposalFactory,
) -> APIRouter:
    router = APIRouter(prefix="/api/crons", tags=["automations"])

    @router.post("/workflow-proposals")
    async def propose_cron_workflow(
        payload: CronWorkflowProposalBody,
    ) -> dict[str, object]:
        try:
            basis = _workflow_basis(payload, kernel, timezone=payload.timezone)
            proposal = await workflow_proposal_factory(payload).propose(basis)
            return proposal.model_dump(mode="json", by_alias=True)
        except WorkflowGenerationError as exc:
            # La cause d'origine (erreur du fournisseur, schema refuse, parsing)
            # n'apparait que dans le chainage : sans trace, seul le message
            # reformule remonte et le diagnostic est perdu.
            _log.exception("Echec de generation du workflow pour %s", payload.agent_id)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (WorkflowValidationError, KernelError, ValueError) as exc:
            _log.warning(
                "Proposition de workflow refusee pour %s : %s", payload.agent_id, exc
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("")
    async def list_crons() -> list[dict[str, object]]:
        return [job.model_dump(mode="json") for job in service.list()]

    @router.get("/{job_id}/approval-status")
    async def cron_approval_status(job_id: str) -> dict[str, object]:
        try:
            job = service.get(job_id)
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        pending = [item for item in kernel.list_approvals() if item.session_id == job.session_id]
        latest_run_id = (
            str(max(pending, key=lambda item: item.created_at).run_id) if pending else None
        )
        latest_pending = [
            item for item in pending if latest_run_id and str(item.run_id) == latest_run_id
        ]
        scopes = sorted(
            kernel.approval_service.approved_scopes(job.session_id),
            key=lambda item: (item[0], item[1], item[2] or ""),
        )
        return {
            "approved_scopes": [
                {"tool_name": tool, "action_family": family, "path": path}
                for tool, family, path in scopes
            ],
            "pending_count": len(latest_pending),
            "pending_run_id": latest_run_id,
        }

    @router.get("/runs")
    async def list_cron_runs(
        job_id: str | None = None,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in service.list_runs(
                job_id=job_id,
                unread_only=unread_only,
                limit=limit,
            )
        ]

    @router.post("/runs/{occurrence_id}/read")
    async def mark_cron_run_read(occurrence_id: str) -> dict[str, object]:
        try:
            return service.mark_run_read(occurrence_id).model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/runs/read-session/{session_id}")
    async def mark_session_runs_read(session_id: str) -> dict[str, object]:
        from uuid import UUID

        try:
            count = service.mark_session_runs_read(UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Session invalide") from exc
        return {"session_id": session_id, "read": count}

    @router.post("")
    async def create_cron(payload: CronCreateBody) -> dict[str, object]:
        try:
            cron_payload = CronJobInput.model_validate(
                payload.model_dump(exclude={"accepted_workflow"})
            )
            accepted = payload.accepted_workflow
            if accepted is None:
                created = service.create(cron_payload)
            else:
                basis = _workflow_basis(
                    cron_payload,
                    kernel,
                    timezone=accepted.workflow.execution.timezone,
                )
                validated = validate_accepted(accepted, basis)
                _validate_enabled_workflow(cron_payload, validated.workflow)
                created = service.create(
                    cron_payload,
                    workflow=validated.workflow.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    workflow_basis_hash=validated.basis_hash,
                )
            return created.model_dump(mode="json")
        except StaleWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (SchedulerError, WorkflowValidationError, KernelError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/{job_id}")
    async def update_cron(  # noqa: C901 - dette: mise à jour multi-cas
        job_id: str, payload: CronJobBody
    ) -> dict[str, object]:
        try:
            previous = service.get(job_id)
            cron_payload = CronJobInput.model_validate(
                payload.model_dump(exclude={"accepted_workflow"})
            )
            accepted = payload.accepted_workflow
            workflow_supersession: dict[str, object] | None = None
            if accepted is not None:
                pending_test = _prepare_workflow_supersession(previous, kernel)
                basis = _workflow_basis(
                    cron_payload,
                    kernel,
                    timezone=accepted.workflow.execution.timezone,
                )
                validated = validate_accepted(accepted, basis)
                _validate_enabled_workflow(cron_payload, validated.workflow)
                updated = service.update_with_workflow(
                    job_id,
                    cron_payload,
                    validated.workflow.model_dump(mode="json", by_alias=True, exclude_none=True),
                    validated.basis_hash,
                )
                workflow_supersession = _complete_workflow_supersession(
                    updated,
                    pending_test,
                    service,
                    kernel,
                )
                if workflow_supersession is not None:
                    updated = service.get(job_id)
            else:
                must_revalidate = _workflow_basis_fields_changed(previous, cron_payload) or (
                    not previous.enabled and cron_payload.enabled
                )
                if previous.workflow is not None and must_revalidate:
                    if not previous.workflow_basis_hash:
                        raise WorkflowValidationError(["workflow_basis_hash absent"])
                    stored = AcceptedWorkflow.model_validate(
                        {
                            "workflow": previous.workflow,
                            "basis_hash": previous.workflow_basis_hash,
                        }
                    )
                    validate_accepted(
                        stored,
                        _workflow_basis(
                            cron_payload,
                            kernel,
                            timezone=stored.workflow.execution.timezone,
                        ),
                    )
                    _validate_enabled_workflow(cron_payload, stored.workflow)
                updated = service.update(job_id, cron_payload)
            permission_changed = any(
                (
                    previous.prompt != updated.prompt,
                    previous.workspace != updated.workspace,
                    previous.agent_id != updated.agent_id,
                    previous.skills != updated.skills,
                    previous.security_mode != updated.security_mode,
                    previous.provider_id != updated.provider_id,
                    previous.model != updated.model,
                    previous.reasoning != updated.reasoning,
                )
            )
            if accepted is not None:
                _revoke_workflow_grants(
                    updated,
                    kernel,
                    "workflow_replaced" if previous.workflow is not None else "workflow_added",
                )
            elif permission_changed:
                kernel.events.append(
                    Event(
                        session_id=updated.session_id,
                        run_id=uuid4(),
                        agent_id=updated.agent_id,
                        type="approval.grants.revoked",
                        payload={"cron_job_id": job_id, "reason": "configuration_changed"},
                    )
                )
            response = updated.model_dump(mode="json")
            if accepted is not None:
                response["workflow_supersession"] = workflow_supersession
            return response
        except StaleWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CronConflictError as exc:
            # Le mapping par sous-chaînes françaises (« introuvable », « pendant
            # une exécution »…) cassait au moindre reformatage de message : les
            # exceptions typées portent la distinction.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (WorkflowValidationError, KernelError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/{job_id}")
    async def delete_cron(job_id: str) -> dict[str, str]:
        try:
            service.delete(job_id)
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": job_id, "status": "deleted"}

    @router.get("/{job_id}/workflow")
    async def get_cron_workflow(job_id: str) -> dict[str, object]:
        try:
            return _workflow_response(service.get(job_id))
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.put("/{job_id}/workflow")
    async def replace_cron_workflow(
        job_id: str,
        payload: CronWorkflowAcceptanceBody,
    ) -> dict[str, object]:
        try:
            job = service.get(job_id)
            pending_test = _prepare_workflow_supersession(job, kernel)
            accepted = payload.accepted_workflow
            basis = _workflow_basis(
                job,
                kernel,
                timezone=accepted.workflow.execution.timezone,
            )
            validated = validate_accepted(accepted, basis)
            _validate_enabled_workflow(job, validated.workflow)
            updated = service.set_workflow(
                job_id,
                validated.workflow.model_dump(mode="json", by_alias=True, exclude_none=True),
                validated.basis_hash,
            )
            workflow_supersession = _complete_workflow_supersession(
                updated,
                pending_test,
                service,
                kernel,
            )
            _revoke_workflow_grants(updated, kernel, "workflow_replaced")
            return {
                **_workflow_response(updated),
                "workflow_supersession": workflow_supersession,
            }
        except StaleWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CronConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (WorkflowValidationError, KernelError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/{job_id}/workflow")
    async def delete_cron_workflow(job_id: str) -> dict[str, object]:
        try:
            job = service.get(job_id)
            if job.workflow is None:
                return {
                    "id": job_id,
                    "status": "absent",
                    **_workflow_response(job),
                }
            _ensure_workflow_mutable(job, kernel)
            updated = service.clear_workflow(job_id)
            _revoke_workflow_grants(updated, kernel, "workflow_deleted")
            return {
                "id": job_id,
                "status": "deleted",
                **_workflow_response(updated),
            }
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CronConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{job_id}/run")
    async def run_cron_now(job_id: str) -> dict[str, str]:
        try:
            job = service.get(job_id)
            _validate_stored_workflow(job, kernel)
        except StaleWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (WorkflowValidationError, KernelError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if job.in_flight or job.blocked:
            detail = (
                "Cette routine attend une autorisation"
                if job.blocked
                else "Ce cronjob est déjà en cours"
            )
            raise HTTPException(status_code=409, detail=detail)
        claimed = service.claim(job_id)
        if claimed is None:
            raise HTTPException(status_code=409, detail="Cronjob désactivé ou déjà en cours")
        task = asyncio.create_task(scheduler._execute(claimed), name=f"amk-cron-{job_id}")
        scheduler._tasks.add(task)
        task.add_done_callback(scheduler._tasks.discard)
        return {
            "id": job_id,
            "status": "started",
            "session_id": str(job.session_id),
            "notification_session_id": str(job.notification_session_id),
            "occurrence_id": claimed.occurrence_id or "",
        }

    @router.post("/{job_id}/test", response_model=RunResult)
    async def test_cron(job_id: str) -> RunResult:
        """Test without advancing schedule, preserving exact guardian grants."""
        try:
            job = service.get(job_id)
            _validate_stored_workflow(job, kernel)
            result = await launch(CronScheduler.request_for(job, trigger="cron_test"))
            # A test does not claim or advance the schedule, but it is still the
            # latest diagnostic result and must clear an obsolete last_error.
            service.record_test_result(job_id, result)
            return result
        except StaleWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (WorkflowValidationError, KernelError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CronNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CronConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


def _workflow_basis(
    payload: CronJobInput | CronJob,
    kernel: Kernel,
    *,
    timezone: str,
) -> WorkflowBasis:
    workspace = payload.workspace.expanduser().resolve() if payload.workspace else None
    agents = kernel.config.agents()
    agent = agents.get(payload.agent_id)
    if agent is None:
        raise WorkflowValidationError([f"agent inconnu : {payload.agent_id}"])
    skills = kernel.config.skills(workspace)
    missing = sorted(set(payload.skills) - skills.keys())
    if missing:
        raise WorkflowValidationError(
            [f"skills inconnues pour ce workspace : {', '.join(missing)}"]
        )
    disabled = kernel.config.disabled_tools(payload.agent_id)
    declared = set(agent.declared_tools)
    effective_catalog = [
        {
            **item,
            "input_schema": guardian_parameters_schema(item.get("input_schema", {})),
        }
        for item in kernel._tool_catalog()
        if item["name"] not in disabled
        and item.get("module") != "kernel"
        and (not declared or item["name"] in declared)
    ]
    return WorkflowBasis(
        name=payload.name.strip(),
        prompt=payload.prompt,
        schedule=payload.schedule.strip(),
        timezone=timezone,
        workspace=str(workspace) if workspace is not None else "",
        agent_id=payload.agent_id,
        skills=payload.skills,
        skill_instructions={name: render_skill(skills, name) for name in payload.skills},
        security_mode=payload.security_mode.value,
        tool_catalog=[WorkflowTool.model_validate(item) for item in effective_catalog],
    )


def _workflow_basis_fields_changed(previous: CronJob, payload: CronJobInput) -> bool:
    """Compare only user-owned fields represented in ``WorkflowBasis``."""

    return any(
        (
            previous.name != payload.name.strip(),
            previous.schedule != payload.schedule.strip(),
            previous.prompt != payload.prompt,
            previous.workspace
            != (payload.workspace.expanduser().resolve() if payload.workspace else None),
            previous.agent_id != payload.agent_id,
            sorted(previous.skills) != sorted(payload.skills),
            previous.security_mode != payload.security_mode,
        )
    )


def _validate_enabled_workflow(
    payload: CronJobInput | CronJob,
    workflow: WorkflowDefinition,
) -> None:
    if payload.enabled and workflow.status != "ready":
        raise WorkflowValidationError(
            [
                f"un workflow {workflow.status} ne peut pas être exécuté; "
                "désactiver la routine ou accepter un workflow ready"
            ]
        )


def _workflow_response(job: CronJob) -> dict[str, object]:
    return {
        "workflow": job.workflow,
        "basis_hash": job.workflow_basis_hash,
        "revision": job.workflow_revision,
        "updated_at": job.workflow_updated_at.isoformat() if job.workflow_updated_at else None,
    }


def _validate_stored_workflow(job: CronJob, kernel: Kernel) -> None:
    if job.workflow is None:
        return
    if not job.workflow_basis_hash:
        raise WorkflowValidationError(["workflow_basis_hash absent"])
    stored = AcceptedWorkflow.model_validate(
        {
            "workflow": job.workflow,
            "basis_hash": job.workflow_basis_hash,
        }
    )
    validated = validate_accepted(
        stored,
        _workflow_basis(
            job,
            kernel,
            timezone=stored.workflow.execution.timezone,
        ),
    )
    if validated.workflow.status != "ready":
        raise WorkflowValidationError(
            [f"le workflow enregistré est {validated.workflow.status} et non exécutable"]
        )


def _ensure_workflow_mutable(job: CronJob, kernel: Kernel) -> None:
    if job.in_flight:
        raise CronConflictError("Impossible de modifier le workflow pendant une exécution")
    if any(item.session_id == job.session_id for item in kernel.list_approvals()):
        raise CronConflictError(
            "Impossible de modifier le workflow pendant une autorisation en attente"
        )


def _prepare_workflow_supersession(
    job: CronJob,
    kernel: Kernel,
) -> CronTestApprovalBatch | None:
    """Allow explicit acceptance to replace only this routine's preflight test."""

    if job.in_flight:
        raise CronConflictError("Impossible de modifier le workflow pendant une exécution")
    if not any(item.session_id == job.session_id for item in kernel.list_approvals()):
        return None
    try:
        batch = kernel.inspect_pending_cron_test(job.session_id, job.id)
    except KernelError as exc:
        raise CronConflictError(
            "Impossible de modifier le workflow pendant une autorisation en attente"
        ) from exc
    if batch is None:
        raise CronConflictError(
            "Impossible de modifier le workflow pendant une autorisation en attente"
        )
    return batch


def _complete_workflow_supersession(
    job: CronJob,
    batch: CronTestApprovalBatch | None,
    service: CronService,
    kernel: Kernel,
) -> dict[str, object] | None:
    if batch is None:
        return None
    try:
        result = kernel.supersede_pending_cron_test(
            batch,
            workflow_revision=job.workflow_revision,
        )
    except KernelError as exc:
        raise CronConflictError(
            "La prévalidation a changé pendant l'acceptation du workflow"
        ) from exc
    service.record_test_result(job.id, result)
    return {
        "run_id": str(result.run_id),
        "status": result.status.value,
        "approval_count": len(batch.approvals),
        "pending_count": 0,
    }


def _revoke_workflow_grants(job: CronJob, kernel: Kernel, reason: str) -> None:
    kernel.events.append(
        Event(
            session_id=job.session_id,
            run_id=uuid4(),
            agent_id=job.agent_id,
            type="approval.grants.revoked",
            payload={"cron_job_id": job.id, "reason": reason},
        )
    )
