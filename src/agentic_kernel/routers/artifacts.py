from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..kernel import Kernel


def _locate(relative: str, recorded: str, artifact_root: Path) -> Path | None:
    """Résout un artefact depuis la racine courante, pas depuis l'ancienne.

    L'adresse d'un artefact est son chemin **relatif** à la racine des artefacts
    de la session. Le dossier de données est déplaçable depuis les paramètres :
    un chemin absolu enregistré devient faux au premier déménagement, et toutes
    les images des conversations passées disparaissent sans message.

    Les journaux antérieurs n'ont que l'absolu. Ses deux derniers segments — le
    run et le fichier — restent valables sous la racine courante, ce qui les
    rattrape sans réécrire le journal.

    La vérification d'appartenance est conservée dans tous les cas : elle
    protège d'un chemin fabriqué, elle n'a jamais eu vocation à figer
    l'emplacement du dossier de données.
    """
    candidats: list[Path] = []
    if relative:
        candidats.append(artifact_root.joinpath(*Path(relative.replace("\\", "/")).parts))
    if recorded:
        segments = Path(recorded.replace("\\", "/")).parts[-2:]
        candidats.extend([artifact_root.joinpath(*segments), Path(recorded)])
    for candidate in candidats:
        try:
            resolu = candidate.resolve()
            resolu.relative_to(artifact_root)
        except (ValueError, OSError):
            continue
        if resolu.is_file():
            return resolu
    return None


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
        artifact_root = (kernel.events.directory / "artifacts" / str(session_id)).resolve()
        path = _locate(
            str(event.payload.get("relative_path") or ""),
            str(event.payload.get("path") or ""),
            artifact_root,
        )
        if path is None:
            raise HTTPException(status_code=404, detail="Fichier artefact introuvable")
        return FileResponse(
            path,
            media_type=str(event.payload.get("media_type", "application/octet-stream")),
            filename=str(event.payload.get("name", path.name)),
        )

    return router
