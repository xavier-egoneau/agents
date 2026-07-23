from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import OAuthManager
from .config import ProjectConfig
from .errors import AuthenticationError, ConfigurationError
from .kernel import Kernel
from .models import (
    ApprovalRequest,
    ImageAttachment,
    ProviderRegistry,
    RunRequest,
    RunResult,
    SecurityMode,
)
from .providers import ProviderFactory


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


class ApprovalResolveBody(BaseModel):
    approved: bool


class ApprovalBatchResolveBody(BaseModel):
    approval_ids: list[str] = Field(min_length=1)
    approved: bool


class SecurityModeBody(BaseModel):
    security_mode: SecurityMode


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
    """Return the complete visible conversation from the latest model snapshot."""
    snapshot_index = -1
    raw_messages: list[dict[str, object]] = []
    for index, event in enumerate(events):
        if event.type == "messages.snapshot":
            candidate = event.payload.get("messages")
            if isinstance(candidate, list):
                snapshot_index = index
                raw_messages = candidate

    visible: list[dict[str, object]] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        for part in raw.get("parts", []):
            if not isinstance(part, dict):
                continue
            part_kind = part.get("part_kind")
            if kind == "request" and part_kind == "user-prompt":
                content = _message_text(part.get("content"))
                if content:
                    visible.append({"role": "user", "content": content})
            elif (
                kind == "response"
                and part_kind == "text"
                and raw.get("finish_reason") != "tool_call"
            ):
                content = _message_text(part.get("content"))
                if content:
                    visible.append({"role": "assistant", "content": content})

    # Preserve turns that failed before Pydantic AI could produce a new snapshot.
    pending_prompt = False
    for event in events[snapshot_index + 1 :]:
        if event.type == "session.started":
            content = _message_text(event.payload.get("prompt"))
            if content:
                visible.append({"role": "user", "content": content})
                pending_prompt = True
        elif event.type == "session.completed" and pending_prompt:
            output = _message_text(event.payload.get("output"))
            errors = event.payload.get("errors", [])
            if not output and isinstance(errors, list):
                output = "\n".join(
                    str(item.get("message", ""))
                    for item in errors
                    if isinstance(item, dict) and item.get("message")
                )
            if output:
                visible.append({
                    "role": "assistant",
                    "content": output,
                    "error": event.payload.get("status") in {"failed", "timeout"},
                })
            pending_prompt = False
    return visible


def create_app(root: Path | str = ".") -> FastAPI:
    project = ProjectConfig(root)
    kernel = Kernel(root)
    running_tasks: dict[UUID, asyncio.Task[RunResult]] = {}
    app = FastAPI(title="Agentic Markdown Kernel", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
            raise HTTPException(status_code=422, detail="Le renommage d’un agent n’est pas implicite")
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
            raise HTTPException(status_code=422, detail="Le renommage d’une skill n’est pas implicite")
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
        references = [
            agent.id for agent in project.agents().values() if skill_id in agent.skills
        ]
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
        return json.loads(
            (project.content_root / "providers.json").read_text(encoding="utf-8")
        )

    def save_provider_document(document: dict[str, object]) -> None:
        ProviderRegistry.model_validate(document)
        target = project.content_root / "providers.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

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
    async def update_provider(
        provider_id: str, payload: ProviderResourceBody
    ) -> dict[str, str]:
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
            item for item in providers
            if not isinstance(item, dict) or item.get("id") != provider_id
        ]
        if len(remaining) == len(providers):
            raise HTTPException(status_code=404, detail="Provider introuvable")
        document["providers"] = remaining
        save_provider_document(document)
        return {"id": provider_id, "status": "deleted"}

    @app.post("/api/runs", response_model=RunResult)
    async def run_agent(payload: WebRunRequest) -> RunResult:
        task = asyncio.create_task(kernel.run(
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
        ))
        running_tasks[payload.session_id] = task
        try:
            return await task
        finally:
            running_tasks.pop(payload.session_id, None)

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

    def session_payload(session_id: UUID) -> dict[str, object]:
        events = kernel.events.read(session_id)
        if not events:
            raise HTTPException(status_code=404, detail="Session not found")
        started = next((event for event in events if event.type == "session.started"), events[0])
        completions = [event for event in events if event.type == "session.completed"]
        latest = completions[-1].payload if completions else {}
        messages = _session_messages(events)
        return {
            "session_id": str(session_id),
            "agent_id": started.agent_id,
            "prompt": started.payload.get("prompt", ""),
            "workspace": started.payload.get("workspace"),
            "created_at": started.timestamp.isoformat(),
            "updated_at": events[-1].timestamp.isoformat(),
            "status": latest.get("status", "running"),
            "output": latest.get("output"),
            "errors": latest.get("errors", []),
            "event_count": len(events),
            "messages": messages,
            "events": [event.model_dump(mode="json") for event in events],
        }

    @app.get("/api/sessions")
    async def list_sessions(workspace: str | None = None) -> list[dict[str, object]]:
        sessions: list[dict[str, object]] = []
        for session_id in kernel.events.list_session_ids():
            payload = session_payload(session_id)
            if workspace is None or payload["workspace"] == str(Path(workspace).expanduser().resolve()):
                sessions.append({
                    key: value
                    for key, value in payload.items()
                    if key not in {"events", "messages"}
                })
        return sessions

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: UUID) -> dict[str, object]:
        return session_payload(session_id)

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
            offset = 0
            idle_ticks = 0
            while not await request.is_disconnected():
                if path.exists():
                    with path.open("r", encoding="utf-8") as source:
                        source.seek(offset)
                        lines = source.readlines()
                        offset = source.tell()
                    for line in lines:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        yield f"event: trace\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
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
        raise ConfigurationError(
            f"Le front matter doit déclarer exactement id: {agent_id}"
        )


def _validate_managed_skill(project: ProjectConfig, skill_id: str, target: Path) -> None:
    skill = project.skills().get(skill_id)
    if skill is None or Path(skill.source).resolve() != target.resolve():
        raise ConfigurationError(
            f"Le front matter doit déclarer exactement name: {skill_id}"
        )
