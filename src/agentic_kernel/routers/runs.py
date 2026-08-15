from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..kernel import Kernel
from ..models import ImageAttachment, RunRequest, RunResult, SecurityMode
from ..scheduler import SchedulerError

LaunchRun = Callable[[RunRequest], Awaitable[RunResult]]


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
    # Sans ces champs, Pydantic ignorait silencieusement la sélection de
    # bibliothèque envoyée par la surface : le mode restait `off` quoi qu'on
    # coche, sans aucune trace.
    knowledge_mode: Literal["off", "auto", "manual"] = "off"
    knowledge_pages: list[str] = Field(default_factory=list)


class ResumeRunBody(BaseModel):
    prompt: str | None = None


class SecurityModeBody(BaseModel):
    security_mode: SecurityMode


def create_run_router(  # noqa: C901 - dette: factory à plusieurs endpoints
    kernel: Kernel,
    running_tasks: dict[UUID, asyncio.Task[Any]],
    launch: LaunchRun,
) -> APIRouter:
    router = APIRouter(prefix="/api/runs", tags=["runs"])

    @router.post("", response_model=RunResult)
    async def run_agent(payload: WebRunRequest) -> RunResult:
        try:
            return await launch(
                RunRequest(
                    prompt=payload.prompt,
                    agent_id=payload.agent_id,
                    skills=payload.skills,
                    workspace=payload.workspace,
                    security_mode=payload.security_mode,
                    session_id=payload.session_id,
                    provider_id=payload.provider_id,
                    model=payload.model,
                    reasoning=payload.reasoning,
                    images=payload.images,
                    knowledge_mode=payload.knowledge_mode,
                    knowledge_pages=payload.knowledge_pages,
                )
            )
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{session_id}/resume", response_model=RunResult)
    async def resume_run(session_id: UUID, payload: ResumeRunBody) -> RunResult:
        events = kernel.events.read(session_id)
        if not events:
            raise HTTPException(status_code=404, detail="Session introuvable")
        completions = [event for event in events if event.type == "session.completed"]
        if not completions:
            raise HTTPException(status_code=409, detail="La session n'est pas arrêtée")
        if completions[-1].payload.get("status") not in {
            "failed",
            "timeout",
            "partial",
            "cancelled",
        }:
            raise HTTPException(
                status_code=409,
                detail="Seules les sessions interrompues peuvent être reprises",
            )
        started = next(
            (event for event in reversed(events) if event.type == "session.started"), None
        )
        if started is None:
            # Un canal sans événement de démarrage (session construite hors run)
            # faisait lever StopIteration et répondait 500 sans diagnostic.
            raise HTTPException(
                status_code=409,
                detail="Cette session ne contient pas d'événement de démarrage à reprendre",
            )
        session = kernel.events.projection.session(session_id)
        logical_workspace = started.payload.get("workspace")
        if session and session.get("trigger") == "agent_channel":
            # Compatibility with events written before logical and effective
            # workspaces were separated: the agent channel was already NULL in
            # the projection even though its event contained the app root.
            logical_workspace = None
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
                    workspace=(
                        Path(str(logical_workspace))
                        if logical_workspace is not None
                        else None
                    ),
                    security_mode=SecurityMode(
                        str(started.payload.get("security_mode") or "limited")
                    ),
                    provider_id=started.payload.get("provider_id"),
                    model=started.payload.get("model"),
                    reasoning=started.payload.get("reasoning"),
                    workflow=started.payload.get("workflow"),
                    tool_allowlist=started.payload.get("tool_allowlist"),
                    trigger="resume",
                )
            )
        except SchedulerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{session_id}/cancel")
    async def cancel_run(session_id: UUID) -> dict[str, str]:
        task = running_tasks.get(session_id)
        if task is None or task.done():
            raise HTTPException(status_code=404, detail="Aucun run actif pour cette session")
        task.cancel()
        return {"status": "cancelling", "session_id": str(session_id)}

    @router.post("/{session_id}/security")
    async def change_run_security(
        session_id: UUID, payload: SecurityModeBody
    ) -> dict[str, str]:
        if not kernel.set_security_mode(session_id, payload.security_mode):
            raise HTTPException(status_code=404, detail="Aucun run actif pour cette session")
        return {"status": "updated", "security_mode": payload.security_mode.value}

    return router
