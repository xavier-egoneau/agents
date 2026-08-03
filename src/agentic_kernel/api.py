from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .auth import OAuthManager
from .config import ProjectConfig
from .errors import AuthenticationError, ConfigurationError
from .git_service import GitService
from .kernel import Kernel
from .models import Event, RunError, RunRequest, RunResult, RunStatus
from .platform.dialogs import NativeDialogUnavailable, choose_directory
from .providers import ProviderFactory
from .routers.approvals import create_approval_router
from .routers.artifacts import create_artifact_router
from .routers.crons import _validate_stored_workflow, create_cron_router
from .routers.files import create_files_router
from .routers.git import create_git_router
from .routers.plans import create_plan_router
from .routers.resources import create_resource_router
from .routers.runs import create_run_router
from .routers.sessions import create_session_router
from .scheduler import (
    ROUTINE_INBOX_SESSION_ID,
    CronJobInput,
    CronScheduler,
    CronService,
    SchedulerError,
)
from .workflows import WorkflowProposalService


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1)


class WorkspaceInfo(BaseModel):
    path: str
    name: str
    readable: bool
    writable: bool


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and isinstance(item.get("content"), str):
                chunks.append(item["content"])
        return "\n".join(chunks)
    return str(value) if value is not None else ""


def _session_messages(events: list) -> list[dict[str, object]]:
    """Reconstruct visible turns, preserving their root run identity."""
    starts = [event for event in events if event.type == "session.started"]
    completions: dict[str, object] = {}
    artifacts: dict[str, list[dict[str, object]]] = {}
    for event in events:
        if event.type == "session.completed":
            completions[str(event.run_id)] = event
        elif event.type == "artifact.created":
            artifacts.setdefault(str(event.run_id), []).append(
                {
                    key: event.payload.get(key)
                    for key in ("artifact_id", "name", "media_type", "kind", "bytes")
                }
            )

    visible: list[dict[str, object]] = []
    for started in starts:
        run_id = str(started.run_id)
        prompt = _message_text(started.payload.get("prompt"))
        if prompt:
            visible.append({"role": "user", "content": prompt, "run_id": run_id})
        completed = completions.get(run_id)
        if completed is None:
            continue
        output = _message_text(completed.payload.get("output"))
        errors = completed.payload.get("errors", [])
        if not output and isinstance(errors, list):
            output = "\n".join(
                str(item.get("message", ""))
                for item in errors
                if isinstance(item, dict) and item.get("message")
            )
        if output:
            visible.append(
                {
                    "role": "assistant",
                    "content": output,
                    "run_id": run_id,
                    "error": completed.payload.get("status") in {"failed", "timeout"},
                    "artifacts": artifacts.get(run_id, []),
                }
            )
    return visible


def create_app(root: Path | str = ".") -> FastAPI:
    project = ProjectConfig(root)
    kernel = Kernel(root)
    git_service = GitService()
    running_tasks: dict[UUID, asyncio.Task[RunResult]] = {}
    cron_service = CronService(project.content_root / "state.db")
    cron_service.import_legacy_once(project.content_root / "agents" / "crons.json", project.root)
    cron_service.repair_orphaned_blocks(
        {item.session_id for item in kernel.list_approvals()}
    )
    routine_inbox = kernel.events.projection.session(ROUTINE_INBOX_SESSION_ID)
    if routine_inbox is None or routine_inbox.get("trigger") != "routine_inbox":
        kernel.events.append(
            Event(
                session_id=ROUTINE_INBOX_SESSION_ID,
                run_id=uuid4(),
                agent_id="main",
                type="routine.inbox.created",
                payload={"prompt": "Routines"},
            )
        )

    def deliver_cron_result(request: RunRequest, result: RunResult) -> None:
        if (
            request.trigger not in {"cron", "cron_resume"}
            or not request.cron_job_id
            or result.status == RunStatus.APPROVAL_PENDING
        ):
            return
        job = cron_service.get(request.cron_job_id)
        target = job.notification_session_id
        if kernel.events.projection.session(target) is None:
            target = ROUTINE_INBOX_SESSION_ID
        error_text = "\n".join(item.message for item in result.errors)
        content = result.output or error_text or f"Routine « {job.name} » terminée."
        kernel.events.append(
            Event(
                session_id=target,
                run_id=result.run_id,
                agent_id=job.agent_id,
                type="routine.notification",
                payload={
                    "cron_job_id": job.id,
                    "name": job.name,
                    "status": result.status.value,
                    "content": f"{job.name}\n\n{content}",
                    "execution_session_id": str(result.session_id),
                },
            )
        )

    def validate_cron_request(request: RunRequest) -> None:
        if request.cron_job_id:
            _validate_stored_workflow(cron_service.get(request.cron_job_id), kernel)

    def with_routine_conversation(request: RunRequest) -> RunRequest:
        """Bring replies made in the delivery session back into the next run.

        Routine executions keep their own durable session for approvals and tool
        traces, while results may be displayed in another conversation. Without
        this bridge, a reply visible below a routine result was silently absent
        from the routine's next model context.
        """
        if request.trigger not in {"cron", "cron_test"} or not request.cron_job_id:
            return request
        job = cron_service.get(request.cron_job_id)
        if job.notification_session_id == job.session_id:
            return request
        events = kernel.events.read(job.notification_session_id)
        last_delivery = -1
        for index, event in enumerate(events):
            if (
                event.type == "routine.notification"
                and event.payload.get("cron_job_id") == job.id
            ):
                last_delivery = index
        replies = [
            _message_text(event.payload.get("prompt"))
            for event in events[last_delivery + 1 :]
            if event.type == "session.started"
            and event.payload.get("trigger", "user") == "user"
        ]
        replies = [item for item in replies if item]
        if not replies:
            return request
        context = "\n\n".join(f"Utilisateur : {item}" for item in replies[-8:])
        return request.model_copy(
            update={
                "prompt": (
                    f"{request.prompt}\n\n"
                    "Contexte récent de la conversation de destination :\n"
                    f"{context}"
                )
            }
        )

    async def launch(request: RunRequest) -> RunResult:
        if request.session_id in running_tasks:
            raise SchedulerError(f"Un run est déjà actif pour la session {request.session_id}")
        validate_cron_request(request)
        request = with_routine_conversation(request)
        task = asyncio.create_task(kernel.run(request))
        running_tasks[request.session_id] = task
        try:
            result = await task
            if request.trigger.startswith("cron") and result.status == RunStatus.SUCCESS:
                tool_failures = [
                    event
                    for event in kernel.events.read(request.session_id)
                    if event.run_id == result.run_id and event.type == "tool.failed"
                ]
                if tool_failures:
                    result = result.model_copy(
                        update={
                            "status": RunStatus.PARTIAL,
                            "errors": [
                                *result.errors,
                                RunError(
                                    type="tool",
                                    message=(
                                        f"{len(tool_failures)} outil(s) ont échoué pendant "
                                        "l’automatisation"
                                    ),
                                    retryable=False,
                                ),
                            ],
                        }
                    )
            deliver_cron_result(request, result)
            return result
        finally:
            running_tasks.pop(request.session_id, None)

    scheduler = CronScheduler(cron_service, launch)

    def workflow_proposal_service(payload: CronJobInput) -> WorkflowProposalService:
        agents = project.agents()
        agent = agents.get(payload.agent_id)
        if agent is None:
            raise ConfigurationError(f"unknown agent: {payload.agent_id}")
        skills = project.skills(payload.workspace)
        creator = skills.get("workflow-creator")
        if creator is None:
            raise ConfigurationError(
                "Le skill workflow-creator doit être installé pour proposer un workflow"
            )
        provider_id = payload.provider_id or agent.provider
        model = ProviderFactory(project.providers()).build(
            provider_id,
            payload.model or agent.model,
        )
        return WorkflowProposalService(
            model,
            workflow_creator=creator.instructions,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler_task = asyncio.create_task(
            scheduler.run_forever(),
            name="amk-cron-scheduler",
        )
        try:
            yield
        finally:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Agentic Markdown Kernel",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1):\d+$",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["content-type"],
    )
    app.include_router(create_run_router(kernel, project.root, running_tasks, launch))
    app.include_router(
        create_cron_router(
            cron_service,
            scheduler,
            launch,
            kernel,
            workflow_proposal_service,
        )
    )
    app.include_router(create_artifact_router(kernel))
    app.include_router(create_resource_router(project))
    app.include_router(create_git_router(project, git_service))
    app.include_router(create_files_router())

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "scheduler": cron_service.scheduler_status()}

    @app.get("/api/context-status")
    async def context_status(
        session_id: UUID | None = None,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        registry = project.providers()
        resolved_provider = provider_id or registry.default_provider
        provider = next(
            (item for item in registry.providers if item.id == resolved_provider),
            None,
        )
        if provider is None:
            raise HTTPException(status_code=404, detail="Provider introuvable")
        return kernel.context_status(
            session_id=session_id,
            provider_id=resolved_provider,
            model_name=model or provider.model,
        )

    def workspace_info(raw_path: str | Path) -> WorkspaceInfo:
        import os

        candidate = Path(raw_path).expanduser()
        resolved = (
            (project.root / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        if not resolved.exists() or not resolved.is_dir():
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422,
                detail=f"Workspace directory not found: {resolved}",
            )
        return WorkspaceInfo(
            path=str(resolved),
            name=resolved.name or str(resolved),
            readable=os.access(resolved, os.R_OK),
            writable=os.access(resolved, os.W_OK),
        )

    @app.get("/api/workspaces/current", response_model=WorkspaceInfo)
    async def current_workspace() -> WorkspaceInfo:
        return workspace_info(project.root)

    @app.post("/api/workspaces/validate", response_model=WorkspaceInfo)
    async def validate_workspace(payload: WorkspaceRequest) -> WorkspaceInfo:
        return workspace_info(payload.path)

    @app.post("/api/workspaces/pick", response_model=WorkspaceInfo)
    async def pick_workspace() -> WorkspaceInfo:
        """Open the host's native directory picker.

        A browser directory input intentionally hides the absolute path. AMK is a
        local application, so the kernel opens the OS picker and returns the
        selected path instead.
        """

        def choose() -> str:
            return choose_directory("Choisir un projet pour AMK")

        try:
            selected = await asyncio.to_thread(choose)
        except (OSError, NativeDialogUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not selected:
            raise HTTPException(status_code=409, detail="Sélection annulée")
        return workspace_info(selected)

    @app.get("/api/catalog")
    async def catalog() -> dict[str, object]:
        providers = project.providers()
        return {
            "default_provider": providers.default_provider,
            "agents": [
                {
                    "id": agent.id,
                    "description": agent.description,
                    "provider": agent.provider,
                    "model": agent.model,
                    "skills": agent.skills,
                    "delegates": agent.delegates,
                }
                for agent in project.agents().values()
            ],
            "skills": [
                {"name": skill.name, "description": skill.description}
                for skill in project.skills().values()
            ],
            "providers": [
                {
                    "id": provider.id,
                    "connection_type": provider.connection_type,
                    "model": provider.model,
                    "models": [
                        model
                        for model in dict.fromkeys([provider.model, *provider.models])
                        if isinstance(model, str) and model
                    ],
                    "vision": bool(getattr(provider, "vision", False)),
                }
                for provider in providers.providers
            ],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "module": module.id,
                    "risks": [risk.value for risk in tool.risk_tags],
                }
                for module in kernel.module_registry.discover().modules
                if module.enabled
                for tool in module.tools
            ],
        }

    @app.get("/api/commands")
    async def commands(workspace: str | None = None) -> list[dict[str, str]]:
        selected = workspace_info(workspace).path if workspace else str(project.root)
        return project.commands(selected)

    app.include_router(
        create_plan_router(project.content_root / "state.db", kernel.events)
    )

    @app.get("/api/providers/{provider_id}/models")
    async def provider_models(provider_id: str) -> dict[str, object]:
        try:
            models, source, error = await ProviderFactory(project.providers()).list_models(
                provider_id
            )
        except ConfigurationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"provider_id": provider_id, "models": models, "source": source, "error": error}

    @app.get("/api/auth/{provider_id}/status")
    async def oauth_status(provider_id: str) -> dict[str, object]:
        try:
            connected = await asyncio.to_thread(OAuthManager().status, provider_id)
        except AuthenticationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"provider_id": provider_id, "connected": connected}

    @app.post("/api/auth/{provider_id}/login")
    async def oauth_login(provider_id: str) -> dict[str, object]:
        """Launch the browser OAuth flow and wait for its localhost callback."""
        try:
            await asyncio.to_thread(
                OAuthManager().login,
                provider_id,
                180,
                allow_manual=False,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"provider_id": provider_id, "connected": True}

    @app.post("/api/auth/{provider_id}/logout")
    async def oauth_logout(provider_id: str) -> dict[str, object]:
        try:
            await asyncio.to_thread(OAuthManager().logout, provider_id)
        except AuthenticationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"provider_id": provider_id, "connected": False}

    app.include_router(create_session_router(kernel, running_tasks))
    app.include_router(
        create_approval_router(
            kernel,
            running_tasks,
            cron_service,
            deliver_cron_result,
            validate_cron_request,
        )
    )

    return app


app = create_app()
