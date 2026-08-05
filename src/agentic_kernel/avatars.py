"""Avatars d'agents : stockage local et validation.

Un agent est un fichier Markdown; son avatar est un fichier image portant son
identifiant, à côté. Rien n'est écrit dans le Markdown : l'image se remplace ou
se supprime sans toucher à la définition de l'agent, et un agent sans avatar
reste parfaitement valide.

L'image sert l'interface de conversation. Côté Telegram, la photo de profil
d'un bot se règle chez BotFather et non par l'API : cet avatar-ci ne la remplace
pas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 1_000_000

# Formats reconnus à leur signature, pas à leur extension : un fichier renommé
# ne doit pas décider de ce qu'on sert ensuite.
SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)

_AGENT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class AvatarError(Exception):
    """Refus explicite; le message est destiné à l'utilisateur."""


@dataclass(frozen=True)
class StoredAvatar:
    path: Path
    media_type: str


def _detect(data: bytes) -> tuple[str, str]:
    for signature, media_type, suffix in SIGNATURES:
        if data.startswith(signature):
            return media_type, suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise AvatarError("format non reconnu : utiliser PNG, JPEG, WebP ou GIF")


class AvatarStore:
    def __init__(self, content_root: Path) -> None:
        self.root = content_root / "agents" / "avatars"

    def _validated_id(self, agent_id: str) -> str:
        # L'identifiant vient de l'URL et sert à construire un chemin : sans
        # cette garde, `../` en sortirait.
        if not _AGENT_ID.fullmatch(agent_id):
            raise AvatarError(f"identifiant d'agent invalide : {agent_id}")
        return agent_id

    def find(self, agent_id: str) -> StoredAvatar | None:
        try:
            self._validated_id(agent_id)
        except AvatarError:
            return None
        for _, media_type, suffix in (*SIGNATURES, (b"", "image/webp", ".webp")):
            candidate = self.root / f"{agent_id}{suffix}"
            if candidate.is_file():
                return StoredAvatar(path=candidate, media_type=media_type)
        return None

    def save(self, agent_id: str, data: bytes) -> StoredAvatar:
        self._validated_id(agent_id)
        if not data:
            raise AvatarError("fichier vide")
        if len(data) > MAX_BYTES:
            raise AvatarError(f"image trop lourde (limite {MAX_BYTES // 1000} ko)")
        media_type, suffix = _detect(data)
        self.root.mkdir(parents=True, exist_ok=True)
        # Un même agent ne garde qu'un avatar : changer de format ne doit pas
        # laisser l'ancien fichier derrière, sinon `find` renverrait le mauvais.
        self.delete(agent_id)
        target = self.root / f"{agent_id}{suffix}"
        target.write_bytes(data)
        return StoredAvatar(path=target, media_type=media_type)

    def delete(self, agent_id: str) -> bool:
        self._validated_id(agent_id)
        removed = False
        for suffix in {suffix for _, _, suffix in SIGNATURES} | {".webp"}:
            candidate = self.root / f"{agent_id}{suffix}"
            if candidate.is_file():
                candidate.unlink()
                removed = True
        return removed

    def agents_with_avatar(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        return {path.stem for path in self.root.iterdir() if path.is_file()}
