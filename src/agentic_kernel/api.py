from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import OAuthManager
from .config import ProjectConfig
from .errors import AuthenticationError, ConfigurationError
from .kernel import Kernel
from .models import (
    ApprovalRequest,
    Event,
    ImageAttachment,
    ProviderRegistry,
    RunRequest,
    RunResult,
    SecurityMode,
)
from .plans import PlanConflict, PlanNotFound, PlanService
from .providers import ProviderFactory
from .scheduler import CronJobInput, CronScheduler, CronService, SchedulerError


class WebRunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    agent_id: str = "main"
    skills: list[str] = Field(default_factory=list)
    workspace: Path | None = None
    security_mode: SecurityMode = SecurityMode.LIMITED
    session_id: UUID = Field(default_factory=uuid4)
    provider_id: str | None = None
    model: str | None = None
    reasoning: str | None = Field(default=None, pattern=r"^(minimal|low|medium|high|xhigh)$")
    images: list[ImageAttachment] = Field(default_factory=list, max_length=4)


class ResumeRunBody(BaseModel):
    prompt: str | None = None


class CronJobBody(CronJobInput):
    pass


class ApprovalResolveBody(BaseModel):
    approved: bool


class ApprovalBatchResolveBody(BaseModel):
    approval_ids: list[str] = Field(min_length=1)
    approved: bool


class SecurityModeBody(BaseModel):
    security_mode: SecurityMode


class PlanStepStatusBody(BaseModel):
    status: str = Field(
        pattern=r"^(pending|claimed|in_progress|validating|completed|blocked|failed)$"
    )


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1)


class WorkspaceInfo(BaseModel):
    path: str
    name: str
    readable: bool
    writable: bool


class MarkdownResourceBody(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    content: str = Field(min_length=1)


class ProviderResourceBody(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    config: dict[str, object]


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
    running_tasks: dict[UUID, asyncio.Task[RunResult]] = {}
    cron_service = CronService(project.content_root / "state.db")
    cron_service.import_legacy_once(project.content_root / "agents" / "crons.json", project.root)

    async def launch(request: RunRequest) -> RunResult:
        if request.session_id in running_tasks:
            raise SchedulerError(f"Un run est déjà actif pour la session {request.session_id}")
        task = asyncio.create_task(kernel.run(request))
        running_tasks[request.session_id] = task
        try:
            return await task
        finally:
            running_tasks.pop(request.session_id, None)

    scheduler = CronScheduler(cron_service, launch)

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
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["content-type"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
        if sys.platform != "darwin":
            raise HTTPException(
                status_code=501,
                detail="Le sélecteur natif de dossier est actuellement disponible sur macOS.",
            )

        def choose() -> str:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'POSIX path of (choose folder with prompt "Choisir un projet pour AMK")',
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                # -128 is the normal AppleScript cancellation error.
                if "User canceled" in result.stderr or "-128" in result.stderr:
                    return ""
                raise RuntimeError(result.stderr.strip() or "Sélecteur de dossier indisponible")
            return result.stdout.strip()

        try:
            selected = await asyncio.to_thread(choose)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
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

    plans = PlanService(project.content_root / "state.db")

    @app.get("/api/plans/current")
    async def current_plan(session_id: UUID) -> dict[str, object] | None:
        return plans.current(session_id)

    @app.patch("/api/plans/{plan_id}/steps/{step_id}")
    async def set_plan_step(
        plan_id: str, step_id: str, payload: PlanStepStatusBody
    ) -> dict[str, object]:
        try:
            plan = plans.update(plan_id, step_id, payload.status)
        except PlanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        kernel.events.append(
            Event(
                session_id=UUID(plan["session_id"]),
                run_id=uuid4(),
                agent_id="user",
                type="plan.updated",
                payload={
                    "plan_id": plan_id,
                    "step_id": step_id,
                    "status": payload.status,
                    "source": "web",
                },
            )
        )
        return plan

    @app.delete("/api/plans/{plan_id}")
    async def delete_plan(plan_id: str) -> dict[str, str]:
        try:
            plan = plans.delete(plan_id)
        except PlanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        kernel.events.append(
            Event(
                session_id=UUID(plan["session_id"]),
                run_id=uuid4(),
                agent_id="user",
                type="plan.deleted",
                payload={"plan_id": plan_id, "source": "web"},
            )
        )
        return {"plan_id": plan_id, "status": "deleted"}

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

    def atomic_markdown_write(target: Path, content: str, validate) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_bytes() if target.exists() else None
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, target)
        try:
            validate()
        except Exception:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                rollback = target.with_suffix(target.suffix + ".rollback")
                rollback.write_bytes(previous)
                os.replace(rollback, target)
            raise

    @app.get("/api/admin/agents")
    async def admin_agents() -> list[dict[str, str]]:
        managed_root = (project.content_root / "agents").resolve()
        return [
            {
                "id": agent.id,
                "description": agent.description,
                "content": Path(agent.source).read_text(encoding="utf-8"),
            }
            for agent in project.agents().values()
            if Path(agent.source).resolve().parent == managed_root
        ]

    @app.post("/api/admin/agents")
    async def create_agent(payload: MarkdownResourceBody) -> dict[str, str]:
        target = project.content_root / "agents" / f"{payload.id}.md"
        if target.exists():
            raise HTTPException(status_code=409, detail=f"L’agent {payload.id} existe déjà")
        try:
            atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_managed_agent(project, payload.id, target),
            )
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": payload.id, "status": "created"}

    @app.put("/api/admin/agents/{agent_id}")
    async def update_agent(agent_id: str, payload: MarkdownResourceBody) -> dict[str, str]:
        if payload.id != agent_id:
            raise HTTPException(
                status_code=422, detail="Le renommage d’un agent n’est pas implicite"
            )
        target = project.content_root / "agents" / f"{agent_id}.md"
        if not target.exists():
            raise HTTPException(status_code=404, detail="Agent introuvable")
        try:
            atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_managed_agent(project, agent_id, target),
            )
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": agent_id, "status": "updated"}

    @app.delete("/api/admin/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> dict[str, str]:
        if agent_id == "main":
            raise HTTPException(status_code=403, detail="L’agent main ne peut pas être supprimé")
        agents = project.agents()
        references = [agent.id for agent in agents.values() if agent_id in agent.delegates]
        if references:
            raise HTTPException(
                status_code=409,
                detail=f"Agent encore référencé par : {', '.join(references)}",
            )
        target = project.content_root / "agents" / f"{agent_id}.md"
        if not target.exists():
            raise HTTPException(status_code=404, detail="Agent introuvable")
        target.unlink()
        (project.content_root / "agents" / f"{agent_id}.tools-disabled.json").unlink(
            missing_ok=True
        )
        return {"id": agent_id, "status": "deleted"}

    @app.get("/api/admin/skills")
    async def admin_skills() -> list[dict[str, str]]:
        return [
            {
                "id": skill.name,
                "description": skill.description,
                "content": Path(skill.source).read_text(encoding="utf-8"),
            }
            for skill in project.skills().values()
        ]

    @app.post("/api/admin/skills")
    async def create_skill(payload: MarkdownResourceBody) -> dict[str, str]:
        target = project.content_root / "skills" / payload.id / "SKILL.md"
        if target.exists():
            raise HTTPException(status_code=409, detail=f"La skill {payload.id} existe déjà")
        try:
            atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_managed_skill(project, payload.id, target),
            )
            project.build_skills_index()
        except ConfigurationError as exc:
            if target.parent.exists() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": payload.id, "status": "created"}

    @app.put("/api/admin/skills/{skill_id}")
    async def update_skill(skill_id: str, payload: MarkdownResourceBody) -> dict[str, str]:
        if payload.id != skill_id:
            raise HTTPException(
                status_code=422, detail="Le renommage d’une skill n’est pas implicite"
            )
        target = project.content_root / "skills" / skill_id / "SKILL.md"
        if not target.exists():
            raise HTTPException(status_code=404, detail="Skill introuvable")
        try:
            atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_managed_skill(project, skill_id, target),
            )
            project.build_skills_index()
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": skill_id, "status": "updated"}

    @app.delete("/api/admin/skills/{skill_id}")
    async def delete_skill(skill_id: str) -> dict[str, str]:
        references = [agent.id for agent in project.agents().values() if skill_id in agent.skills]
        if references:
            raise HTTPException(
                status_code=409,
                detail=f"Skill encore référencée par : {', '.join(references)}",
            )
        target = project.content_root / "skills" / skill_id
        if not (target / "SKILL.md").exists():
            raise HTTPException(status_code=404, detail="Skill introuvable")
        shutil.rmtree(target)
        project.build_skills_index()
        return {"id": skill_id, "status": "deleted"}

    def provider_document() -> dict[str, object]:
        return json.loads((project.content_root / "providers.json").read_text(encoding="utf-8"))

    def save_provider_document(document: dict[str, object]) -> None:
        ProviderRegistry.model_validate(document)
        target = project.content_root / "providers.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        target.chmod(0o600)

    @app.get("/api/admin/providers")
    async def admin_providers() -> dict[str, object]:
        document = provider_document()
        sanitized = []
        for raw in document.get("providers", []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["api_key_configured"] = bool(item.get("api_key"))
            item["api_key"] = ""
            sanitized.append(item)
        return {
            "default_provider": document.get("default_provider"),
            "providers": sanitized,
        }

    @app.post("/api/admin/providers")
    async def create_provider(payload: ProviderResourceBody) -> dict[str, str]:
        document = provider_document()
        providers = document.get("providers", [])
        if not isinstance(providers, list):
            raise HTTPException(status_code=422, detail="Registre providers invalide")
        if any(isinstance(item, dict) and item.get("id") == payload.id for item in providers):
            raise HTTPException(status_code=409, detail="Provider déjà existant")
        config = {**payload.config, "id": payload.id}
        config.pop("api_key_configured", None)
        providers.append(config)
        try:
            save_provider_document(document)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": payload.id, "status": "created"}

    @app.put("/api/admin/providers/{provider_id}")
    async def update_provider(provider_id: str, payload: ProviderResourceBody) -> dict[str, str]:
        if payload.id != provider_id:
            raise HTTPException(status_code=422, detail="Le renommage n’est pas implicite")
        document = provider_document()
        providers = document.get("providers", [])
        for index, existing in enumerate(providers):
            if isinstance(existing, dict) and existing.get("id") == provider_id:
                update = dict(payload.config)
                update.pop("api_key_configured", None)
                if not update.get("api_key"):
                    update["api_key"] = existing.get("api_key")
                providers[index] = {**existing, **update, "id": provider_id}
                break
        else:
            raise HTTPException(status_code=404, detail="Provider introuvable")
        try:
            save_provider_document(document)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": provider_id, "status": "updated"}

    @app.delete("/api/admin/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> dict[str, str]:
        document = provider_document()
        if document.get("default_provider") == provider_id:
            raise HTTPException(
                status_code=409, detail="Le provider par défaut ne peut pas être supprimé"
            )
        references = [
            agent.id for agent in project.agents().values() if agent.provider == provider_id
        ]
        if references:
            raise HTTPException(
                status_code=409,
                detail=f"Provider encore utilisé par : {', '.join(references)}",
            )
        providers = document.get("providers", [])
        remaining = [
            item
            for item in providers
            if not isinstance(item, dict) or item.get("id") != provider_id
        ]
        if len(remaining) == len(providers):
            raise HTTPException(status_code=404, detail="Provider introuvable")
        document["providers"] = remaining
        save_provider_document(document)
        return {"id": provider_id, "status": "deleted"}

    @app.post("/api/runs", response_model=RunResult)
    async def run_agent(payload: WebRunRequest) -> RunResult:
        try:
            return await launch(
                RunRequest(
                    prompt=payload.prompt,
                    agent_id=payload.agent_id,
                    skills=payload.skills,
                    workspace=payload.workspace or project.root,
                    security_mode=payload.security_mode,
                    session_id=payload.session_id,
                    provider_id=payload.provider_id,
                    model=payload.model,
                    reasoning=payload.reasoning,
                    images=payload.images,
                )
            )
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{session_id}/resume", response_model=RunResult)
    async def resume_run(session_id: UUID, payload: ResumeRunBody) -> RunResult:
        events = kernel.events.read(session_id)
        if not events:
            raise HTTPException(status_code=404, detail="Session introuvable")
        completions = [event for event in events if event.type == "session.completed"]
        if not completions:
            raise HTTPException(status_code=409, detail="La session n'est pas arrêtée")
        latest = completions[-1].payload
        if latest.get("status") not in {"failed", "timeout", "partial", "cancelled"}:
            raise HTTPException(
                status_code=409, detail="Seules les sessions interrompues peuvent être reprises"
            )
        started = next(event for event in reversed(events) if event.type == "session.started")
        prompt = payload.prompt or (
            "Reprends la tâche interrompue à partir des traces, artefacts et résultats "
            "déjà persistés. Ne rejoue pas les outils déjà terminés. Vérifie l'état "
            "courant et poursuis par la prochaine action utile."
        )
        try:
            return await launch(
                RunRequest(
                    prompt=prompt,
                    agent_id=started.agent_id,
                    skills=list(started.payload.get("skills", [])),
                    session_id=session_id,
                    workspace=Path(str(started.payload.get("workspace") or project.root)),
                    security_mode=SecurityMode(
                        str(started.payload.get("security_mode") or "limited")
                    ),
                    provider_id=started.payload.get("provider_id"),
                    model=started.payload.get("model"),
                    reasoning=started.payload.get("reasoning"),
                    trigger="resume",
                )
            )
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{session_id}/cancel")
    async def cancel_run(session_id: UUID) -> dict[str, str]:
        task = running_tasks.get(session_id)
        if task is None or task.done():
            raise HTTPException(status_code=404, detail="Aucun run actif pour cette session")
        task.cancel()
        return {"status": "cancelling", "session_id": str(session_id)}

    @app.post("/api/runs/{session_id}/security")
    async def change_run_security(session_id: UUID, payload: SecurityModeBody) -> dict[str, str]:
        if not kernel.set_security_mode(session_id, payload.security_mode):
            raise HTTPException(status_code=404, detail="Aucun run actif pour cette session")
        return {"status": "updated", "security_mode": payload.security_mode.value}

    @app.get("/api/crons")
    async def list_crons() -> list[dict[str, object]]:
        return [job.model_dump(mode="json") for job in cron_service.list()]

    @app.post("/api/crons")
    async def create_cron(payload: CronJobBody) -> dict[str, object]:
        try:
            return cron_service.create(payload).model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/crons/{job_id}")
    async def update_cron(job_id: str, payload: CronJobBody) -> dict[str, object]:
        try:
            return cron_service.update(job_id, payload).model_dump(mode="json")
        except SchedulerError as exc:
            raise HTTPException(
                status_code=404 if "introuvable" in str(exc) else 422, detail=str(exc)
            ) from exc

    @app.delete("/api/crons/{job_id}")
    async def delete_cron(job_id: str) -> dict[str, str]:
        try:
            cron_service.delete(job_id)
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": job_id, "status": "deleted"}

    @app.post("/api/crons/{job_id}/run")
    async def run_cron_now(job_id: str) -> dict[str, str]:
        try:
            job = cron_service.get(job_id)
        except SchedulerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if job.in_flight:
            raise HTTPException(status_code=409, detail="Ce cronjob est déjà en cours")
        # Move next fire normally while executing an explicit manual fire.
        claimed = cron_service.claim(job_id)
        if claimed is None:
            raise HTTPException(status_code=409, detail="Cronjob désactivé ou déjà en cours")
        task = asyncio.create_task(scheduler._execute(claimed), name=f"amk-cron-{job_id}")
        scheduler._tasks.add(task)
        task.add_done_callback(scheduler._tasks.discard)
        return {"id": job_id, "status": "started", "session_id": str(job.session_id)}

    @app.post("/api/crons/{job_id}/test", response_model=RunResult)
    async def test_cron(job_id: str) -> RunResult:
        """Run a routine without advancing its schedule.

        The durable routine session is deliberately reused: exact guardian
        grants approved during the test therefore remain valid for subsequent
        scheduled executions.
        """
        try:
            job = cron_service.get(job_id)
            return await launch(CronScheduler.request_for(job, trigger="cron_test"))
        except SchedulerError as exc:
            status = 404 if "introuvable" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    def read_event_page(
        session_id: UUID,
        *,
        limit: int,
        before_sequence: int | None = None,
    ) -> list[dict[str, object]]:
        positions = kernel.events.projection.event_positions(
            session_id,
            limit=limit,
            before_sequence=before_sequence,
        )
        path = kernel.events.path_for(session_id)
        if not positions or not path.exists():
            return []
        result: list[dict[str, object]] = []
        with path.open("rb") as source:
            for position in positions:
                source.seek(position["source_offset"])
                try:
                    payload = json.loads(source.read(position["source_length"]))
                except json.JSONDecodeError:
                    continue
                payload["sequence"] = position["sequence"]
                result.append(payload)
        return result

    def session_payload(
        session_id: UUID,
        *,
        message_limit: int = 200,
        event_limit: int = 500,
    ) -> dict[str, object]:
        session = kernel.events.projection.session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = kernel.events.projection.messages(
            session_id,
            limit=max(1, min(message_limit, 500)),
        )
        events = read_event_page(
            session_id,
            limit=max(1, min(event_limit, 1000)),
        )
        return {
            **{
                key: value
                for key, value in session.items()
                if key not in {"errors_json", "last_sequence"}
            },
            "messages": [
                {
                    "role": message["role"],
                    "content": message["content"],
                    "run_id": message["run_id"],
                    "error": message["status"] in {"failed", "timeout"},
                    "artifacts": message["artifacts"],
                }
                for message in messages
            ],
            "events": events,
            "messages_has_more": len(messages) == max(1, min(message_limit, 500)),
            "events_has_more": len(events) == max(1, min(event_limit, 1000)),
        }

    @app.get("/api/sessions")
    async def list_sessions(
        workspace: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        resolved_workspace = str(Path(workspace).expanduser().resolve()) if workspace else None
        rows = kernel.events.projection.list_sessions(
            resolved_workspace,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"errors_json", "last_sequence"}
            }
            for row in rows
        ]

    @app.get("/api/sessions/{session_id}")
    async def get_session(
        session_id: UUID,
        message_limit: int = 200,
        event_limit: int = 500,
    ) -> dict[str, object]:
        return session_payload(
            session_id,
            message_limit=message_limit,
            event_limit=event_limit,
        )

    @app.get("/api/sessions/{session_id}/event-page")
    async def get_event_page(
        session_id: UUID,
        limit: int = 200,
        before_sequence: int | None = None,
    ) -> dict[str, object]:
        events = read_event_page(
            session_id,
            limit=max(1, min(limit, 1000)),
            before_sequence=before_sequence,
        )
        return {
            "events": events,
            "has_more": len(events) == max(1, min(limit, 1000)),
        }

    @app.get("/api/sessions/{session_id}/messages")
    async def get_messages(
        session_id: UUID,
        limit: int = 100,
        before_sequence: int | None = None,
    ) -> dict[str, object]:
        messages = kernel.events.projection.messages(
            session_id,
            limit=max(1, min(limit, 500)),
            before_sequence=before_sequence,
        )
        return {
            "messages": messages,
            "has_more": len(messages) == max(1, min(limit, 500)),
        }

    @app.get("/api/artifacts/{session_id}/{artifact_id}")
    async def get_artifact(session_id: UUID, artifact_id: str) -> FileResponse:
        event = next(
            (
                item
                for item in reversed(kernel.events.read(session_id))
                if item.type == "artifact.created"
                and item.payload.get("artifact_id") == artifact_id
            ),
            None,
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Artefact introuvable")
        path = Path(str(event.payload.get("path", ""))).resolve()
        artifact_root = (kernel.events.directory / "artifacts" / str(session_id)).resolve()
        try:
            path.relative_to(artifact_root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Artefact hors périmètre") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Fichier artefact introuvable")
        return FileResponse(
            path,
            media_type=str(event.payload.get("media_type", "application/octet-stream")),
            filename=str(event.payload.get("name", path.name)),
        )

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: UUID) -> dict[str, str]:
        if session_id in running_tasks:
            raise HTTPException(status_code=409, detail="Impossible de supprimer un run actif")
        if not kernel.events.delete(session_id):
            raise HTTPException(status_code=404, detail="Session introuvable")
        kernel.approvals.remove_for_session(session_id)
        return {"session_id": str(session_id), "status": "deleted"}

    @app.get("/api/sessions/{session_id}/events")
    async def stream_session_events(session_id: UUID, request: Request) -> StreamingResponse:
        path = kernel.events.path_for(session_id)

        async def stream():
            raw_last = request.headers.get("last-event-id", "0")
            try:
                sequence = max(0, int(raw_last))
            except ValueError:
                sequence = 0
            idle_ticks = 0
            while not await request.is_disconnected():
                positions = kernel.events.projection.positions_after(
                    session_id,
                    sequence,
                )
                if positions and path.exists():
                    with path.open("rb") as source:
                        for position in positions:
                            source.seek(position["source_offset"])
                            line = source.read(position["source_length"])
                            sequence = position["sequence"]
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            yield (
                                f"id: {sequence}\n"
                                f"event: trace\n"
                                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                            )
                            idle_ticks = 0
                idle_ticks += 1
                if idle_ticks % 15 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/approvals", response_model=list[ApprovalRequest])
    async def list_approvals() -> list[ApprovalRequest]:
        return kernel.list_approvals()

    @app.post("/api/approvals/{approval_id}/resolve", response_model=RunResult)
    async def resolve_approval(approval_id: str, payload: ApprovalResolveBody) -> RunResult:
        state = kernel.approvals.load_state(UUID(approval_id))
        session_id = UUID(str(state["approval"]["session_id"])) if state else None
        task = asyncio.create_task(kernel.resolve_approval(approval_id, payload.approved))
        if session_id:
            running_tasks[session_id] = task
        try:
            return await task
        finally:
            if session_id:
                running_tasks.pop(session_id, None)

    @app.post("/api/approvals/resolve-batch", response_model=RunResult)
    async def resolve_approval_batch(payload: ApprovalBatchResolveBody) -> RunResult:
        state = kernel.approvals.load_state(UUID(payload.approval_ids[0]))
        session_id = UUID(str(state["approval"]["session_id"])) if state else None
        task = asyncio.create_task(
            kernel.resolve_approval_batch(payload.approval_ids, payload.approved)
        )
        if session_id:
            running_tasks[session_id] = task
        try:
            return await task
        finally:
            if session_id:
                running_tasks.pop(session_id, None)

    return app


app = create_app()


def _validate_managed_agent(project: ProjectConfig, agent_id: str, target: Path) -> None:
    agent = project.agents().get(agent_id)
    if agent is None or Path(agent.source).resolve() != target.resolve():
        raise ConfigurationError(f"Le front matter doit déclarer exactement id: {agent_id}")


def _validate_managed_skill(project: ProjectConfig, skill_id: str, target: Path) -> None:
    skill = project.skills().get(skill_id)
    if skill is None or Path(skill.source).resolve() != target.resolve():
        raise ConfigurationError(f"Le front matter doit déclarer exactement name: {skill_id}")
