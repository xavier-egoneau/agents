from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..kernel import Kernel
from ..models import Event
from ..session_lifecycle import SessionLifecycle


def create_session_router(  # noqa: C901 - dette: factory à plusieurs endpoints
    kernel: Kernel,
    running_tasks: dict[UUID, asyncio.Task[Any]],
    state_database: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])
    lifecycle = SessionLifecycle(kernel, state_database)

    def public_session(row: dict[str, object]) -> dict[str, object]:
        logical_workspace = row.get("workspace")
        agent_id = str(row.get("agent_id") or "main")
        effective = kernel.config.resolve_workspace(agent_id, logical_workspace)
        return {
            **{
                key: value
                for key, value in row.items()
                if key not in {"errors_json", "last_sequence"}
            },
            "effective_workspace": str(effective),
            "workspace_kind": "project" if logical_workspace else "agent_default",
        }

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

    @router.get("")
    async def list_sessions(
        workspace: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_automations: bool = False,
        include_channels: bool = False,
    ) -> list[dict[str, object]]:
        resolved_workspace = str(Path(workspace).expanduser().resolve()) if workspace else None
        default_workspace_agent: str | None = None
        if resolved_workspace:
            default_root = (kernel.config.content_root / "workspaces").resolve()
            candidate = Path(resolved_workspace)
            if candidate.parent == default_root:
                agent_id = candidate.name
                if agent_id in kernel.config.agents():
                    default_workspace_agent = agent_id
        rows = kernel.events.projection.list_sessions(
            resolved_workspace,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
            include_automations=include_automations,
            include_channels=include_channels,
            default_workspace_agent=default_workspace_agent,
        )
        return [public_session(row) for row in rows]

    @router.get("/{session_id}")
    async def get_session(
        session_id: UUID,
        message_limit: int = 200,
        event_limit: int = 500,
    ) -> dict[str, object]:
        session = kernel.events.projection.session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        message_limit = max(1, min(message_limit, 500))
        event_limit = max(1, min(event_limit, 1000))
        messages = kernel.events.projection.messages(session_id, limit=message_limit)
        events = read_event_page(session_id, limit=event_limit)
        return {
            **public_session(session),
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
            "messages_has_more": len(messages) == message_limit,
            "events_has_more": len(events) == event_limit,
        }

    @router.get("/{session_id}/event-page")
    async def get_event_page(
        session_id: UUID,
        limit: int = 200,
        before_sequence: int | None = None,
    ) -> dict[str, object]:
        limit = max(1, min(limit, 1000))
        events = read_event_page(
            session_id,
            limit=limit,
            before_sequence=before_sequence,
        )
        return {"events": events, "has_more": len(events) == limit}

    @router.get("/{session_id}/messages")
    async def get_messages(
        session_id: UUID,
        limit: int = 100,
        before_sequence: int | None = None,
    ) -> dict[str, object]:
        limit = max(1, min(limit, 500))
        messages = kernel.events.projection.messages(
            session_id,
            limit=limit,
            before_sequence=before_sequence,
        )
        return {"messages": messages, "has_more": len(messages) == limit}

    @router.delete("/{session_id}")
    async def delete_session(session_id: UUID) -> dict[str, str]:
        # Le canal d'un agent est permanent : c'est son fil, pas une
        # conversation. Le supprimer le ferait réapparaître vide au prochain
        # démarrage, en ayant perdu son historique pour rien.
        canal = kernel.events.projection.session(session_id)
        if canal is not None and canal.get("trigger") == "agent_channel":
            raise HTTPException(
                status_code=409,
                detail=f"Le canal de {canal.get('agent_id', 'agent')} est permanent",
            )
        if session_id in running_tasks:
            raise HTTPException(status_code=409, detail="Impossible de supprimer un run actif")
        if not lifecycle.delete(session_id):
            raise HTTPException(status_code=404, detail="Session introuvable")
        return {"session_id": str(session_id), "status": "deleted"}

    @router.post("/{session_id}/clear")
    async def clear_session(session_id: UUID) -> dict[str, str]:
        session = kernel.events.projection.session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session introuvable")
        if session_id in running_tasks:
            raise HTTPException(status_code=409, detail="Un run est encore en cours")
        # Le canal d'un agent n'est pas une conversation : il se recrée avec son
        # propre événement, qui rétablit son titre et son absence de workspace.
        # Toute autre session est vidée en conservant ce qui la définit — son
        # déclencheur, son projet, sa visibilité.
        est_canal = session.get("trigger") == "agent_channel"
        lifecycle.reset(
            Event(
                session_id=session_id,
                run_id=uuid4(),
                agent_id=str(session.get("agent_id", "main")),
                type="agent.channel.created" if est_canal else "session.cleared",
                payload=(
                    {"prompt": str(session.get("prompt") or session.get("agent_id") or "agent")}
                    if est_canal
                    else {
                        "prompt": str(session.get("prompt") or ""),
                        "workspace": session.get("workspace"),
                        "trigger": str(session.get("trigger") or "user"),
                        "hidden": bool(session.get("hidden")),
                    }
                ),
            )
        )
        return {"session_id": str(session_id), "status": "cleared"}

    @router.get("/{session_id}/events")
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
                positions = kernel.events.projection.positions_after(session_id, sequence)
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
                                "event: trace\n"
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

    return router
