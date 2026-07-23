from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from uuid import UUID

from .models import Event


class JsonlEventStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = Lock()

    def append(self, event: Event) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(event.session_id)
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        with self._lock, path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def read(self, session_id: UUID) -> list[Event]:
        path = self.path_for(session_id)
        if not path.exists():
            return []
        return [Event.model_validate_json(line) for line in path.read_text().splitlines() if line]

    def list_session_ids(self) -> list[UUID]:
        """Return persisted sessions, newest file first.

        Approval state files share this directory, so only UUID-named JSONL files are
        considered part of the event log.
        """
        if not self.directory.exists():
            return []
        candidates: list[tuple[int, UUID]] = []
        for path in self.directory.glob("*.jsonl"):
            try:
                session_id = UUID(path.stem)
            except ValueError:
                continue
            candidates.append((path.stat().st_mtime_ns, session_id))
        return [session_id for _, session_id in sorted(candidates, reverse=True)]

    def path_for(self, session_id: UUID) -> Path:
        return self.directory / f"{session_id}.jsonl"

    def delete(self, session_id: UUID) -> bool:
        path = self.path_for(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True
