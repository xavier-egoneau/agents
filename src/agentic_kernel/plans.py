from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

PlanStepStatus = Literal[
    "pending",
    "claimed",
    "in_progress",
    "validating",
    "completed",
    "blocked",
    "failed",
]


class PlanStepInput(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    dependencies: list[str] = Field(default_factory=list)
    parallelizable: bool = False
    write_scopes: list[str] = Field(default_factory=list)


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
        db.execute(
            """CREATE TABLE IF NOT EXISTS plan_steps (
                plan_id TEXT NOT NULL, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                title TEXT NOT NULL, status TEXT NOT NULL, dependencies_json TEXT NOT NULL,
                parallelizable INTEGER NOT NULL, write_scopes_json TEXT NOT NULL,
                note TEXT, claimed_by TEXT, claimed_at TEXT, lease_until TEXT, run_id TEXT,
                validation_json TEXT,
                PRIMARY KEY(plan_id, step_id),
                FOREIGN KEY(plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
            )"""
        )
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(plan_steps)").fetchall()
        }
        if "validation_json" not in columns:
            db.execute("ALTER TABLE plan_steps ADD COLUMN validation_json TEXT")
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
                raise PlanError(f"unknown dependencies for {ids[index]}: {sorted(unknown)}")
            if ids[index] in dependencies:
                raise PlanError(f"step {ids[index]} cannot depend on itself")
            scopes = list(
                dict.fromkeys(scope.strip() for scope in step.write_scopes if scope.strip())
            )
            if step.parallelizable and not scopes:
                raise PlanError(f"parallel step {ids[index]} requires at least one write scope")
            payload.append(
                {
                    "id": ids[index],
                    "title": step.title.strip(),
                    "status": "pending",
                    "dependencies": dependencies,
                    "parallelizable": step.parallelizable,
                    "write_scopes": scopes,
                    "note": None,
                    "claimed_by": None,
                    "claimed_at": None,
                    "lease_until": None,
                    "run_id": None,
                    "validation": None,
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
            self._replace_steps(db, plan_id, payload)
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
                row = db.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM plans WHERE plan_id = ? AND session_id = ?",
                    (plan_id, str(session_id)),
                ).fetchone()
        if row is None:
            raise PlanNotFound("plan not found")
        rendered = self._render(row)
        with self._db() as db:
            normalized = self._read_steps(db, plan_id)
            if not normalized:
                self._replace_steps(db, plan_id, rendered["steps"])
                normalized = self._read_steps(db, plan_id)
        return {**rendered, "steps": normalized}

    def current(self, session_id: UUID | str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM plans WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        return self.get(row["plan_id"], session_id) if row is not None else None

    def ready(self, plan_id: str, session_id: UUID | str) -> dict[str, Any]:
        self.release_expired_claims(plan_id)
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
                raise PlanConflict("step dependencies are not completed: " + ", ".join(incomplete))
        if target.get("status") == "validating" and status == "completed":
            raise PlanConflict(
                "a delegated step must be completed with plan_validate and evidence"
            )
        target.update(status=status, note=note)
        if status in {"completed", "blocked", "failed"}:
            target.update(
                claimed_by=None,
                claimed_at=None,
                lease_until=None,
            )
        now = datetime.now(UTC).isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE plans SET steps_json = ?, updated_at = ? WHERE plan_id = ?",
                (json.dumps(steps), now, plan_id),
            )
            self._replace_steps(db, plan_id, steps)
        return {**plan, "steps": steps, "updated_at": now}

    def validate(
        self,
        plan_id: str,
        step_id: str,
        *,
        session_id: UUID | str,
        passed: bool,
        evidence: list[str],
        note: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a delegated validating step from explicit parent evidence."""
        cleaned = list(dict.fromkeys(item.strip() for item in evidence if item.strip()))
        if not cleaned:
            raise PlanConflict("validation requires at least one evidence item")
        plan = self.get(plan_id, session_id)
        target = next((step for step in plan["steps"] if step["id"] == step_id), None)
        if target is None:
            raise PlanNotFound("step not found")
        if target["status"] != "validating":
            raise PlanConflict(f"step {step_id} is not awaiting validation")
        now = datetime.now(UTC).isoformat()
        target.update(
            status="completed" if passed else "failed",
            note=note,
            validation={
                "passed": passed,
                "evidence": cleaned,
                "validated_at": now,
            },
            claimed_by=None,
            claimed_at=None,
            lease_until=None,
        )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE plans SET steps_json=?, updated_at=? WHERE plan_id=?",
                (json.dumps(plan["steps"]), now, plan_id),
            )
            self._replace_steps(db, plan_id, plan["steps"])
        return {**plan, "updated_at": now}

    def claim(
        self,
        plan_id: str,
        step_id: str,
        *,
        session_id: UUID | str,
        claimed_by: str,
        run_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        self.release_expired_claims(plan_id, now=now)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM plans WHERE plan_id=? AND session_id=?",
                (plan_id, str(session_id)),
            ).fetchone()
            if row is None:
                raise PlanNotFound("plan not found")
            plan = {**self._render(row), "steps": self._read_steps(db, plan_id)}
            by_id = {step["id"]: step for step in plan["steps"]}
            target = by_id.get(step_id)
            if target is None:
                raise PlanNotFound("step not found")
            if target["status"] != "pending":
                raise PlanConflict(f"step {step_id} is not pending")
            incomplete = [
                dependency
                for dependency in target["dependencies"]
                if by_id[dependency]["status"] != "completed"
            ]
            if incomplete:
                raise PlanConflict("step dependencies are not completed: " + ", ".join(incomplete))
            if target.get("parallelizable"):
                scopes = target.get("write_scopes", [])
                if not scopes:
                    raise PlanConflict("parallel step has no write scope")
                active = [
                    step
                    for step in plan["steps"]
                    if step["status"] in {"claimed", "in_progress", "validating"}
                ]
                conflict = next(
                    (
                        step["id"]
                        for step in active
                        if _scopes_overlap(scopes, step.get("write_scopes", []))
                    ),
                    None,
                )
                if conflict:
                    raise PlanConflict(f"write scope overlaps active step {conflict}")
            target.update(
                status="claimed",
                claimed_by=claimed_by,
                claimed_at=now.isoformat(),
                lease_until=(now + timedelta(seconds=lease_seconds)).isoformat(),
                run_id=run_id,
            )
            self._replace_steps(db, plan_id, plan["steps"])
            db.execute(
                "UPDATE plans SET steps_json=?, updated_at=? WHERE plan_id=?",
                (json.dumps(plan["steps"]), now.isoformat(), plan_id),
            )
        return {**plan, "updated_at": now.isoformat()}

    def release_expired_claims(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        current = (now or datetime.now(UTC)).isoformat()
        with self._db() as db:
            rows = db.execute(
                """SELECT step_id FROM plan_steps
                   WHERE plan_id=? AND status IN ('claimed', 'in_progress', 'validating')
                   AND lease_until IS NOT NULL AND lease_until < ?""",
                (plan_id, current),
            ).fetchall()
            if rows:
                db.execute(
                    """UPDATE plan_steps SET status='pending', claimed_by=NULL,
                       claimed_at=NULL, lease_until=NULL, run_id=NULL
                       WHERE plan_id=?
                       AND status IN ('claimed', 'in_progress', 'validating')
                       AND lease_until < ?""",
                    (plan_id, current),
                )
                steps = self._read_steps(db, plan_id)
                db.execute(
                    "UPDATE plans SET steps_json=?, updated_at=? WHERE plan_id=?",
                    (json.dumps(steps), current, plan_id),
                )
        return len(rows)

    def delete(self, plan_id: str) -> dict[str, Any]:
        plan = self.get(plan_id)
        with self._db() as db:
            db.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
        return plan

    def delete_for_session(self, session_id: UUID | str) -> int:
        with self._db() as db:
            plan_ids = [
                row["plan_id"]
                for row in db.execute(
                    "SELECT plan_id FROM plans WHERE session_id = ?", (str(session_id),)
                )
            ]
            if plan_ids:
                placeholders = ",".join("?" for _ in plan_ids)
                db.execute(
                    f"DELETE FROM plan_steps WHERE plan_id IN ({placeholders})", plan_ids
                )
            cursor = db.execute("DELETE FROM plans WHERE session_id = ?", (str(session_id),))
        return cursor.rowcount

    @staticmethod
    def _replace_steps(
        db: sqlite3.Connection,
        plan_id: str,
        steps: list[dict[str, Any]],
    ) -> None:
        db.execute("DELETE FROM plan_steps WHERE plan_id=?", (plan_id,))
        db.executemany(
            """INSERT INTO plan_steps
               (plan_id, step_id, ordinal, title, status, dependencies_json,
                parallelizable, write_scopes_json, note, claimed_by, claimed_at,
                lease_until, run_id, validation_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    plan_id,
                    step["id"],
                    index,
                    step["title"],
                    step.get("status", "pending"),
                    json.dumps(step.get("dependencies", [])),
                    int(bool(step.get("parallelizable"))),
                    json.dumps(step.get("write_scopes", [])),
                    step.get("note"),
                    step.get("claimed_by"),
                    step.get("claimed_at"),
                    step.get("lease_until"),
                    step.get("run_id"),
                    (
                        json.dumps(step["validation"])
                        if step.get("validation") is not None
                        else None
                    ),
                )
                for index, step in enumerate(steps)
            ],
        )

    @staticmethod
    def _read_steps(
        db: sqlite3.Connection,
        plan_id: str,
    ) -> list[dict[str, Any]]:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY ordinal",
            (plan_id,),
        ).fetchall()
        return [
            {
                "id": row["step_id"],
                "title": row["title"],
                "status": row["status"],
                "dependencies": json.loads(row["dependencies_json"]),
                "parallelizable": bool(row["parallelizable"]),
                "write_scopes": json.loads(row["write_scopes_json"]),
                "note": row["note"],
                "claimed_by": row["claimed_by"],
                "claimed_at": row["claimed_at"],
                "lease_until": row["lease_until"],
                "run_id": row["run_id"],
                "validation": (
                    json.loads(row["validation_json"])
                    if row["validation_json"]
                    else None
                ),
            }
            for row in rows
        ]


def _scopes_overlap(left: list[str], right: list[str]) -> bool:
    for first in left:
        first_path = Path(first)
        for second in right:
            second_path = Path(second)
            if first_path == second_path:
                return True
            if first_path in second_path.parents or second_path in first_path.parents:
                return True
    return False
