from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from uuid import UUID

from .models import Event
from .projections import SessionProjection


class JsonlEventStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = Lock()
        self.projection = SessionProjection(directory.parent / "state.db")

    def append(self, event: Event) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(event.session_id)
        line = (
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        with self._lock, path.open("ab") as stream:
            source_offset = stream.tell()
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
            self.projection.apply(
                event,
                source_offset=source_offset,
                source_length=len(line),
            )

    def rebuild_projection(self) -> None:
        if not self.directory.exists():
            return
        for session_id in self.list_session_ids():
            path = self.path_for(session_id)
            with path.open("rb") as stream:
                offset = self.projection.source_offset(session_id)
                if offset > path.stat().st_size:
                    offset = 0
                stream.seek(offset)
                while True:
                    source_offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        event = Event.model_validate_json(line)
                    except ValueError:
                        continue
                    self.projection.apply(
                        event,
                        source_offset=source_offset,
                        source_length=len(line),
                    )
            if self.projection.needs_message_backfill(session_id):
                self.projection.backfill_messages(session_id, self.read(session_id))

    def read(self, session_id: UUID) -> list[Event]:
        path = self.path_for(session_id)
        if not path.exists():
            return []
        return [
            Event.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

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
        self.projection.delete_session(session_id)
        return True

    def reset(self, event: Event) -> None:
        """Atomically replace a session log, then transactionally reproject it."""
        line = (
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(event.session_id)
        with self._lock:
            temporary = path.with_suffix(".jsonl.tmp")
            with temporary.open("wb") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self.projection.reset(event, source_length=len(line))
