from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from .events import JsonlEventStore
from .models import Event, RunRequest
from .vision import LocalVisionService, VisionUnavailable


class VisionPreparation:
    """Archive les images jointes et les décrit via la vision locale.

    Quand le modèle actif ne voit pas les images, le kernel les archive quand
    même et les fait décrire par le serveur de vision local : l'agent reçoit
    des observations textuelles au lieu d'un contenu qu'il ne peut pas lire.
    """

    def __init__(self, events: JsonlEventStore, vision: LocalVisionService) -> None:
        self.events = events
        self.vision = vision

    def archive_input_images(self, request: RunRequest, run_id) -> list[tuple[Any, bytes, Path]]:
        archived: list[tuple[Any, bytes, Path]] = []
        artifact_root = self.events.directory / "artifacts" / str(request.session_id)
        for index, image in enumerate(request.images, 1):
            try:
                raw = base64.b64decode(image.data_base64, validate=True)
            except ValueError as exc:
                raise VisionUnavailable("image encodée invalide") from exc
            artifact_id = uuid4().hex
            directory = artifact_root / artifact_id
            directory.mkdir(parents=True, exist_ok=True)
            safe_name = Path(image.name).name or f"image-{index}.png"
            target = directory / safe_name
            target.write_bytes(raw)
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="artifact.created",
                    payload={
                        "artifact_id": artifact_id,
                        "name": safe_name,
                        "media_type": image.media_type,
                        "kind": "input_image",
                        "bytes": len(raw),
                        # Voir `routers/artifacts.py` : le relatif est l'adresse,
                        # l'absolu n'est qu'une trace de production.
                        "relative_path": f"{artifact_id}/{safe_name}",
                        "path": str(target),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                )
            )
            archived.append((image, raw, target))
        return archived

    async def prepare_with_local_vision(
        self,
        request: RunRequest,
        run_id,
        archived: list[tuple[Any, bytes, Path]],
    ) -> RunRequest:
        observations: list[str] = []
        for index, (image, raw, target) in enumerate(archived, 1):
            artifact_id = target.parent.name
            safe_name = target.name
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="tool.started",
                    payload={
                        "tool_name": "image_inspect",
                        "automatic": True,
                        "artifact_id": artifact_id,
                        "path": str(target),
                    },
                )
            )
            try:
                observation = await self.vision.analyze_bytes(
                    raw,
                    image.media_type,
                    (f"Analyse cette image pour répondre à la demande suivante : {request.prompt}"),
                    "balanced",
                )
            except VisionUnavailable as exc:
                self.events.append(
                    Event(
                        session_id=request.session_id,
                        run_id=run_id,
                        agent_id="kernel",
                        type="tool.failed",
                        payload={
                            "tool_name": "image_inspect",
                            "automatic": True,
                            "artifact_id": artifact_id,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
                observations.append(
                    f"## Image {index}: {safe_name}\n"
                    "Analyse visuelle indisponible. L’image est bien jointe et archivée, "
                    "mais son contenu n’a pas été observé. Ne déduis aucun détail visuel et "
                    f"signale cette limite à l’utilisateur. Cause locale : {exc}"
                )
                continue
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="tool.completed",
                    payload={
                        "tool_name": "image_inspect",
                        "automatic": True,
                        "artifact_id": artifact_id,
                        "observation_chars": len(observation),
                        "preview": observation[:500],
                    },
                )
            )
            observations.append(
                f"## Image {index}: {safe_name}\n"
                f"Artifact reference: `{artifact_id}`\n\n{observation}"
            )
        augmented = "\n\n".join(
            [
                request.prompt,
                (
                    "# Local vision observations\n\n"
                    "The active model is text-only. The notes below state whether each "
                    "attachment was analyzed locally. Use only recorded observations, "
                    "never infer unavailable visual content, and state any limitation."
                ),
                *observations,
            ]
        )
        return request.model_copy(update={"prompt": augmented, "images": []})
