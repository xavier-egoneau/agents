from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

PlanStepStatus = Literal["pending", "in_progress", "completed", "blocked", "failed"]


class PlanStepInput(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    dependencies: list[str] = Field(default_factory=list)
    parallelizable: bool = False


class PlanError(ValueError):
    pass


class PlanNotFound(PlanError):
    pass


class PlanConflict(PlanError):
    pass


class PlanService:
    """Single source of truth for plan validation and persistence."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)

    def _db(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute(
            """CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                steps_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        return db

    @staticmethod
    def _render(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "plan_id": row["plan_id"],
            "session_id": row["session_id"],
            "title": row["title"],
            "steps": json.loads(row["steps_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create(
        self, session_id: UUID | str, title: str, steps: list[PlanStepInput]
    ) -> dict[str, Any]:
        if not title.strip() or not steps or len(steps) > 100:
            raise PlanError("title and 1..100 non-empty steps are required")
        ids = [step.id or f"T{index + 1}" for index, step in enumerate(steps)]
        if len(ids) != len(set(ids)):
            raise PlanError("plan step ids must be unique")
        known = set(ids)
        payload: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            dependencies = list(dict.fromkeys(step.dependencies))
            unknown = set(dependencies) - known
            if unknown:
                raise PlanError(
                    f"unknown dependencies for {ids[index]}: {sorted(unknown)}"
                )
            if ids[index] in dependencies:
                raise PlanError(f"step {ids[index]} cannot depend on itself")
            payload.append(
                {
                    "id": ids[index],
                    "title": step.title.strip(),
                    "status": "pending",
                    "dependencies": dependencies,
                    "parallelizable": step.parallelizable,
                    "note": None,
                }
            )
        self._validate_acyclic(payload)
        plan_id, now = str(uuid4()), datetime.now(UTC).isoformat()
        with self._db() as db:
            db.execute(
                "INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    str(session_id),
                    title.strip(),
                    json.dumps(payload),
                    now,
                    now,
                ),
            )
        return {
            "plan_id": plan_id,
            "session_id": str(session_id),
            "title": title.strip(),
            "steps": payload,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _validate_acyclic(steps: list[dict[str, Any]]) -> None:
        dependencies = {item["id"]: item["dependencies"] for item in steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise PlanError(f"cyclic plan dependency involving {step_id}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in dependencies:
            visit(step_id)

    def get(self, plan_id: str, session_id: UUID | str | None = None) -> dict[str, Any]:
        with self._db() as db:
            if session_id is None:
                row = db.execute(
                    "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM plans WHERE plan_id = ? AND session_id = ?",
                    (plan_id, str(session_id)),
                ).fetchone()
        if row is None:
            raise PlanNotFound("plan not found")
        return self._render(row)

    def current(self, session_id: UUID | str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        return self._render(row) if row is not None else None

    def ready(self, plan_id: str, session_id: UUID | str) -> dict[str, Any]:
        plan = self.get(plan_id, session_id)
        by_id = {step["id"]: step for step in plan["steps"]}
        ready = [
            step
            for step in plan["steps"]
            if step["status"] == "pending"
            and all(
                by_id[dependency]["status"] == "completed"
                for dependency in step.get("dependencies", [])
            )
        ]
        return {
            "plan_id": plan_id,
            "ready": ready,
            "parallel": [step for step in ready if step.get("parallelizable")],
            "sequential": [step for step in ready if not step.get("parallelizable")],
            "remaining": sum(
                step["status"] not in {"completed", "failed"} for step in plan["steps"]
            ),
        }

    def update(
        self,
        plan_id: str,
        step_id: str,
        status: PlanStepStatus,
        *,
        session_id: UUID | str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        plan = self.get(plan_id, session_id)
        steps = plan["steps"]
        target = next((step for step in steps if step["id"] == step_id), None)
        if target is None:
            raise PlanNotFound("step not found")
        if status in {"in_progress", "completed"}:
            by_id = {step["id"]: step for step in steps}
            incomplete = [
                dependency
                for dependency in target.get("dependencies", [])
                if by_id[dependency]["status"] != "completed"
            ]
            if incomplete:
                raise PlanConflict(
                    "step dependencies are not completed: " + ", ".join(incomplete)
                )
        target.update(status=status, note=note)
        now = datetime.now(UTC).isoformat()
        with self._db() as db:
            db.execute(
                "UPDATE plans SET steps_json = ?, updated_at = ? WHERE plan_id = ?",
                (json.dumps(steps), now, plan_id),
            )
        return {**plan, "steps": steps, "updated_at": now}

    def delete(self, plan_id: str) -> dict[str, Any]:
        plan = self.get(plan_id)
        with self._db() as db:
            db.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
        return plan
