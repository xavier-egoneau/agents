from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .errors import ConfigurationError


class SnapshotStore:
    """Content-addressed compressed model-history snapshots."""

    def __init__(self, sessions_root: Path) -> None:
        self.root = sessions_root / "blobs"

    def save(self, session_id: UUID, messages: list[Any]) -> dict[str, Any]:
        raw = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.root / str(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest}.json.gz"
        if not target.exists():
            temporary = directory / f".{digest}.{uuid4().hex}.tmp"
            try:
                with gzip.open(temporary, "wb", compresslevel=6) as stream:
                    stream.write(raw)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "blob": str(target.relative_to(self.root.parent)),
            "sha256": digest,
            "bytes": len(raw),
            "compressed_bytes": target.stat().st_size,
            "message_count": len(messages),
            "estimated_tokens": max(1, round(len(raw) / 3.5)),
        }

    def load(self, session_id: UUID, payload: dict[str, Any]) -> list[Any] | None:
        inline = payload.get("messages")
        if isinstance(inline, list):
            return inline
        raw_path = payload.get("blob")
        digest = payload.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(digest, str):
            return None
        candidate = (self.root.parent / raw_path).resolve()
        expected_root = (self.root / str(session_id)).resolve()
        try:
            candidate.relative_to(expected_root)
        except ValueError as exc:
            raise ConfigurationError("snapshot blob is outside its session") from exc
        try:
            with gzip.open(candidate, "rb") as stream:
                raw = stream.read()
        except OSError as exc:
            raise ConfigurationError(f"unreadable snapshot blob: {candidate}") from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ConfigurationError(f"snapshot hash mismatch: {candidate}")
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ConfigurationError(f"invalid snapshot payload: {candidate}")
        return value

    def clear(self, session_id: UUID) -> None:
        directory = (self.root / str(session_id)).resolve()
        directory.relative_to(self.root.resolve())
        shutil.rmtree(directory, ignore_errors=True)
