from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

KanbanStatus = Literal["backlog", "ready", "running", "review", "done", "blocked"]
KanbanPriority = Literal["low", "medium", "high", "urgent"]


class KanbanError(ValueError):
    pass


class KanbanNotFound(KanbanError):
    pass


class KanbanConflict(KanbanError):
    pass


class KanbanTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    workspace: str = Field(min_length=1)
    prompt: str = Field(default="", max_length=100_000)
    acceptance_criteria: str = Field(default="", max_length=30_000)
    status: KanbanStatus = "backlog"
    priority: KanbanPriority = "medium"
    agent_id: str = "main"
    skills: list[str] = Field(default_factory=list)
    security_mode: Literal["safe", "limited", "power"] = "limited"
    provider_id: str | None = None
    model: str | None = None
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    auto_run: bool = False
    dependencies: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def executable_tasks_need_a_prompt(self) -> KanbanTaskInput:
        if (self.status == "ready" or self.auto_run) and not self.prompt.strip():
            raise ValueError("une tâche prête ou automatique doit contenir un prompt")
        return self


class KanbanTaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=240)
    prompt: str | None = Field(default=None, max_length=100_000)
    acceptance_criteria: str | None = Field(default=None, max_length=30_000)
    status: KanbanStatus | None = None
    priority: KanbanPriority | None = None
    agent_id: str | None = None
    skills: list[str] | None = None
    security_mode: Literal["safe", "limited", "power"] | None = None
    provider_id: str | None = None
    model: str | None = None
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    auto_run: bool | None = None
    dependencies: list[str] | None = None


class KanbanService:
    """Persistent project task board with atomic claims for automated workers."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)

    def _db(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """CREATE TABLE IF NOT EXISTS kanban_tasks (
                id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                skills_json TEXT NOT NULL,
                security_mode TEXT NOT NULL,
                provider_id TEXT,
                model TEXT,
                reasoning TEXT NOT NULL,
                auto_run INTEGER NOT NULL DEFAULT 0,
                dependencies_json TEXT NOT NULL,
                position INTEGER NOT NULL,
                claimed_by TEXT,
                claimed_at TEXT,
                lease_until TEXT,
                run_id TEXT,
                session_id TEXT,
                last_result TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_kanban_workspace_status
               ON kanban_tasks(workspace, status, position)"""
        )
        return db

    @staticmethod
    def _render(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "skills": json.loads(row["skills_json"]),
            "dependencies": json.loads(row["dependencies_json"]),
            "auto_run": bool(row["auto_run"]),
        } | {"skills_json": None, "dependencies_json": None}

    @staticmethod
    def _clean(task: dict[str, Any]) -> dict[str, Any]:
        task.pop("skills_json", None)
        task.pop("dependencies_json", None)
        return task

    def list(self, workspace: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT * FROM kanban_tasks WHERE workspace=?
                   ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                     WHEN 'medium' THEN 2 ELSE 3 END, position, created_at""",
                (workspace,),
            ).fetchall()
        return [self._clean(self._render(row)) for row in rows]

    def get(self, task_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM kanban_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KanbanNotFound("tâche Kanban introuvable")
        return self._clean(self._render(row))

    def create(self, payload: KanbanTaskInput) -> dict[str, Any]:
        task_id = f"task_{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        with self._db() as db:
            position = db.execute(
                """SELECT COALESCE(MAX(position), -1) + 1 FROM kanban_tasks
                   WHERE workspace=? AND status=?""",
                (payload.workspace, payload.status),
            ).fetchone()[0]
            db.execute(
                """INSERT INTO kanban_tasks (
                    id, workspace, title, prompt, acceptance_criteria, status, priority,
                    agent_id, skills_json, security_mode, provider_id, model, reasoning,
                    auto_run, dependencies_json, position, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, payload.workspace, payload.title.strip(), payload.prompt.strip(),
                    payload.acceptance_criteria.strip(), payload.status, payload.priority,
                    payload.agent_id, json.dumps(payload.skills), payload.security_mode,
                    payload.provider_id, payload.model, payload.reasoning, int(payload.auto_run),
                    json.dumps(payload.dependencies), position, now, now,
                ),
            )
        return self.get(task_id)

    def update(self, task_id: str, patch: KanbanTaskPatch) -> dict[str, Any]:
        current = self.get(task_id)
        values = patch.model_dump(exclude_unset=True, exclude_none=True)
        if not values:
            return current
        merged = {**current, **values}
        KanbanTaskInput.model_validate({key: merged[key] for key in KanbanTaskInput.model_fields})
        if task_id in merged["dependencies"]:
            raise KanbanConflict("une tâche ne peut pas dépendre d’elle-même")
        unknown = [
            dependency
            for dependency in merged["dependencies"]
            if not self._exists(dependency)
        ]
        if unknown:
            raise KanbanConflict(f"dépendances inconnues : {unknown}")
        columns, parameters = [], []
        for key, value in values.items():
            column = {"skills": "skills_json", "dependencies": "dependencies_json"}.get(key, key)
            if key in {"skills", "dependencies"}:
                value = json.dumps(value)
            elif key == "auto_run":
                value = int(value)
            columns.append(f"{column}=?")
            parameters.append(value.strip() if isinstance(value, str) else value)
        columns.append("updated_at=?")
        parameters.extend([datetime.now(UTC).isoformat(), task_id])
        with self._db() as db:
            db.execute(
                f"UPDATE kanban_tasks SET {', '.join(columns)} WHERE id=?",  # noqa: S608
                parameters,
            )
        return self.get(task_id)

    def _exists(self, task_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT 1 FROM kanban_tasks WHERE id=?", (task_id,)).fetchone()
            return row is not None

    def delete(self, task_id: str) -> None:
        with self._db() as db:
            cursor = db.execute("DELETE FROM kanban_tasks WHERE id=?", (task_id,))
        if cursor.rowcount == 0:
            raise KanbanNotFound("tâche Kanban introuvable")

    def release_expired_claims(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self._db() as db:
            cursor = db.execute(
                """UPDATE kanban_tasks SET status='ready', claimed_by=NULL,
                   claimed_at=NULL, lease_until=NULL, updated_at=?
                   WHERE status='running' AND lease_until IS NOT NULL AND lease_until < ?""",
                (now, now),
            )
        return cursor.rowcount

    def claim_next(
        self, workspace: str, worker_id: str, *, lease_seconds: int = 1800,
        automatic_only: bool = True,
    ) -> dict[str, Any] | None:
        self.release_expired_claims()
        now = datetime.now(UTC)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = db.execute(
                """SELECT * FROM kanban_tasks WHERE workspace=? AND status='ready'
                   AND (?=0 OR auto_run=1)
                   ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                     WHEN 'medium' THEN 2 ELSE 3 END, position, created_at""",
                (workspace, int(automatic_only)),
            ).fetchall()
            chosen = None
            for row in candidates:
                dependencies = json.loads(row["dependencies_json"])
                if not dependencies:
                    chosen = row
                    break
                placeholders = ",".join("?" for _ in dependencies)
                statuses = db.execute(
                    f"SELECT id, status FROM kanban_tasks WHERE id IN ({placeholders})",  # noqa: S608
                    dependencies,
                ).fetchall()
                if len(statuses) == len(dependencies) and all(
                    item["status"] == "done" for item in statuses
                ):
                    chosen = row
                    break
            if chosen is None:
                db.commit()
                return None
            claimed_at = now.isoformat()
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            db.execute(
                """UPDATE kanban_tasks SET status='running', claimed_by=?, claimed_at=?,
                   lease_until=?, updated_at=? WHERE id=? AND status='ready'""",
                (worker_id, claimed_at, lease_until, claimed_at, chosen["id"]),
            )
            db.commit()
        return self.get(chosen["id"])

    def finish(
        self, task_id: str, status: Literal["review", "done", "blocked"], *,
        result: str = "", error: str = "", run_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.get(task_id)
        now = datetime.now(UTC).isoformat()
        with self._db() as db:
            db.execute(
                """UPDATE kanban_tasks SET status=?, claimed_by=NULL, claimed_at=NULL,
                   lease_until=NULL, run_id=?, session_id=?, last_result=?, last_error=?,
                   updated_at=? WHERE id=?""",
                (status, run_id, session_id, result[:20_000], error[:10_000], now, task_id),
            )
        return self.get(task_id)
