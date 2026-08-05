from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..kernel import Kernel


def create_artifact_router(kernel: Kernel) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

    @router.get("/{session_id}/{artifact_id}")
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

    return router
