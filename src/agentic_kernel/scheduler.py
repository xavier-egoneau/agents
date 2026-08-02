from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from .models import RunRequest, RunResult, RunStatus, SecurityMode
from .workflows import WorkflowDefinition, workflow_tool_allowlist

ROUTINE_INBOX_SESSION_ID = uuid5(NAMESPACE_URL, "agentic-kernel:routine-inbox")


class CronJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    schedule: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1)
    workspace: Path | None = None
    agent_id: str = "main"
    skills: list[str] = Field(default_factory=list)
    security_mode: SecurityMode = SecurityMode.LIMITED
    provider_id: str | None = None
    model: str | None = None
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    enabled: bool = True
    auto_resume: bool = True
    notification_session_id: UUID = ROUTINE_INBOX_SESSION_ID


class CronJob(CronJobInput):
    id: str
    session_id: UUID
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_retryable: bool = False
    in_flight: bool = False
    blocked: bool = False
    occurrence_id: str | None = None
    scheduled_for: datetime | None = None
    workflow: dict[str, Any] | None = None
    workflow_revision: int = 0
    workflow_basis_hash: str | None = None
    workflow_updated_at: datetime | None = None


class CronRun(BaseModel):
    id: str
    cron_job_id: str
    scheduled_for: datetime
    claimed_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    session_id: UUID
    notification_session_id: UUID
    run_id: UUID | None = None
    execution_status: str
    task_status: str | None = None
    delivery_status: str = "unread"
    output_preview: str | None = None
    error: str | None = None


class SchedulerError(ValueError):
    pass


class CronService:
    """Persistent cron definitions and runtime state.

    SQLite is the source of structured state; each actual run still writes to
    the normal append-only session JSONL through ``Kernel.run``.
    """

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    skills_json TEXT NOT NULL,
                    security_mode TEXT NOT NULL,
                    provider_id TEXT,
                    model TEXT,
                    reasoning TEXT,
                    enabled INTEGER NOT NULL,
                    auto_resume INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    last_retryable INTEGER NOT NULL DEFAULT 0,
                    in_flight INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    workflow_json TEXT,
                    workflow_revision INTEGER NOT NULL DEFAULT 0,
                    workflow_basis_hash TEXT,
                    workflow_updated_at TEXT
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(cron_jobs)")}
            if "last_retryable" not in columns:
                connection.execute(
                    "ALTER TABLE cron_jobs ADD COLUMN last_retryable INTEGER NOT NULL DEFAULT 0"
                )
            if "blocked" not in columns:
                connection.execute(
                    "ALTER TABLE cron_jobs ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
                )
            if "notification_session_id" not in columns:
                connection.execute("ALTER TABLE cron_jobs ADD COLUMN notification_session_id TEXT")
            if "workflow_json" not in columns:
                connection.execute("ALTER TABLE cron_jobs ADD COLUMN workflow_json TEXT")
            if "workflow_revision" not in columns:
                connection.execute(
                    "ALTER TABLE cron_jobs ADD COLUMN workflow_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "workflow_basis_hash" not in columns:
                connection.execute("ALTER TABLE cron_jobs ADD COLUMN workflow_basis_hash TEXT")
            if "workflow_updated_at" not in columns:
                connection.execute("ALTER TABLE cron_jobs ADD COLUMN workflow_updated_at TEXT")
            # A previous process may have added the nullable column and stopped
            # before backfilling every row. Keep this repair idempotent instead
            # of tying it only to the ALTER TABLE branch.
            connection.execute(
                "UPDATE cron_jobs SET notification_session_id=? "
                "WHERE notification_session_id IS NULL OR notification_session_id=''",
                (str(ROUTINE_INBOX_SESSION_ID),),
            )
            connection.execute("""
                CREATE TABLE IF NOT EXISTS cron_runs (
                    id TEXT PRIMARY KEY,
                    cron_job_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    execution_status TEXT NOT NULL,
                    task_status TEXT,
                    delivery_status TEXT NOT NULL DEFAULT 'unread',
                    output_preview TEXT,
                    error TEXT,
                    FOREIGN KEY(cron_job_id) REFERENCES cron_jobs(id) ON DELETE CASCADE,
                    UNIQUE(cron_job_id, scheduled_for)
                )
            """)
            run_columns = {row[1] for row in connection.execute("PRAGMA table_info(cron_runs)")}
            if "notification_session_id" not in run_columns:
                connection.execute("ALTER TABLE cron_runs ADD COLUMN notification_session_id TEXT")
            connection.execute(
                "UPDATE cron_runs SET notification_session_id=? "
                "WHERE notification_session_id IS NULL OR notification_session_id=''",
                (str(ROUTINE_INBOX_SESSION_ID),),
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_cron_runs_job_claimed
                   ON cron_runs(cron_job_id, claimed_at DESC)"""
            )
            # Repair the legacy cron-test bug: a test could mark a job blocked
            # even though no scheduled occurrence was waiting for approval.
            connection.execute(
                """UPDATE cron_jobs SET blocked=0
                   WHERE blocked=1 AND NOT EXISTS (
                     SELECT 1 FROM cron_runs
                     WHERE cron_runs.cron_job_id=cron_jobs.id
                       AND cron_runs.execution_status='blocked'
                   )"""
            )
            connection.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

    def import_legacy_once(self, path: Path, default_workspace: Path) -> int:
        """Import Cody-compatible crons.json once, without deleting the source."""
        with self._connect() as connection:
            migrated = connection.execute(
                "SELECT value FROM scheduler_meta WHERE key='legacy_crons_imported'"
            ).fetchone()
        if migrated is not None:
            return 0
        imported = 0
        if path.is_file():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                jobs = document.get("jobs", []) if isinstance(document, dict) else []
            except (OSError, json.JSONDecodeError):
                jobs = []
            for item in jobs:
                if not isinstance(item, dict):
                    continue
                try:
                    self.create(
                        CronJobInput(
                            name=str(item.get("name") or "Routine importée"),
                            schedule=str(item.get("schedule") or ""),
                            prompt=str(item.get("message") or item.get("prompt") or ""),
                            workspace=Path(item.get("workspace") or default_workspace),
                            agent_id=str(item.get("agent") or "main"),
                            enabled=bool(item.get("enabled", True)),
                            auto_resume=True,
                        )
                    )
                    imported += 1
                except (SchedulerError, ValueError):
                    continue
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO scheduler_meta(key, value)
                   VALUES('legacy_crons_imported', ?)""",
                (datetime.now(UTC).isoformat(),),
            )
        return imported

    @staticmethod
    def validate_schedule(expression: str) -> None:
        if not croniter.is_valid(expression):
            raise SchedulerError(f"Expression cron invalide : {expression}")

    @staticmethod
    def next_fire(expression: str, base: datetime | None = None) -> datetime:
        CronService.validate_schedule(expression)
        reference = base or datetime.now().astimezone()
        result = croniter(expression, reference).get_next(datetime)
        return result if result.tzinfo else result.replace(tzinfo=reference.tzinfo)

    def create(
        self,
        payload: CronJobInput,
        *,
        workflow: dict[str, Any] | None = None,
        workflow_basis_hash: str | None = None,
    ) -> CronJob:
        self.validate_schedule(payload.schedule)
        workspace = payload.workspace.expanduser().resolve() if payload.workspace else None
        if workspace is not None and not workspace.is_dir():
            raise SchedulerError(f"Workspace introuvable : {workspace}")
        now = datetime.now(UTC)
        job_id = f"cron_{uuid4().hex}"
        session_id = uuid4()
        next_run = self.next_fire(payload.schedule)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO cron_jobs (
                    id, name, schedule, prompt, workspace, agent_id, skills_json,
                    security_mode, provider_id, model, reasoning, enabled, auto_resume,
                    session_id, created_at, updated_at, next_run_at, last_run_at,
                    last_status, last_error, last_retryable, in_flight,
                    notification_session_id, workflow_json, workflow_revision,
                    workflow_basis_hash, workflow_updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0,
                    ?, ?, ?, ?, ?
                )""",
                (
                    job_id,
                    payload.name.strip(),
                    payload.schedule.strip(),
                    payload.prompt,
                    str(workspace) if workspace is not None else "",
                    payload.agent_id,
                    json.dumps(payload.skills),
                    payload.security_mode.value,
                    payload.provider_id,
                    payload.model,
                    payload.reasoning,
                    int(payload.enabled),
                    int(payload.auto_resume),
                    str(session_id),
                    now.isoformat(),
                    now.isoformat(),
                    next_run.isoformat(),
                    None,
                    None,
                    None,
                    str(payload.notification_session_id),
                    json.dumps(workflow, ensure_ascii=False) if workflow is not None else None,
                    1 if workflow is not None else 0,
                    workflow_basis_hash if workflow is not None else None,
                    now.isoformat() if workflow is not None else None,
                ),
            )
        return self.get(job_id)

    def list(self) -> list[CronJob]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM cron_jobs ORDER BY created_at DESC").fetchall()
        return [self._row(row) for row in rows]

    def get(self, job_id: str) -> CronJob:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise SchedulerError(f"Cronjob introuvable : {job_id}")
        return self._row(row)

    def update(self, job_id: str, payload: CronJobInput) -> CronJob:
        self.get(job_id)
        self.validate_schedule(payload.schedule)
        workspace = payload.workspace.expanduser().resolve() if payload.workspace else None
        if workspace is not None and not workspace.is_dir():
            raise SchedulerError(f"Workspace introuvable : {workspace}")
        now = datetime.now(UTC)
        next_run = self.next_fire(payload.schedule)
        with self._connect() as connection:
            connection.execute(
                """UPDATE cron_jobs SET name=?, schedule=?, prompt=?, workspace=?,
                   agent_id=?, skills_json=?, security_mode=?, provider_id=?, model=?,
                   reasoning=?, enabled=?, auto_resume=?, updated_at=?, next_run_at=?,
                   notification_session_id=?
                   WHERE id=?""",
                (
                    payload.name.strip(),
                    payload.schedule.strip(),
                    payload.prompt,
                    str(workspace) if workspace is not None else "",
                    payload.agent_id,
                    json.dumps(payload.skills),
                    payload.security_mode.value,
                    payload.provider_id,
                    payload.model,
                    payload.reasoning,
                    int(payload.enabled),
                    int(payload.auto_resume),
                    now.isoformat(),
                    next_run.isoformat(),
                    str(payload.notification_session_id),
                    job_id,
                ),
            )
        return self.get(job_id)

    def update_with_workflow(
        self,
        job_id: str,
        payload: CronJobInput,
        workflow: dict[str, Any],
        workflow_basis_hash: str,
    ) -> CronJob:
        """Atomically update a routine basis and its explicitly accepted workflow."""

        job = self.get(job_id)
        if job.in_flight:
            raise SchedulerError("Impossible de modifier le workflow pendant une exécution")
        self.validate_schedule(payload.schedule)
        workspace = payload.workspace.expanduser().resolve() if payload.workspace else None
        if workspace is not None and not workspace.is_dir():
            raise SchedulerError(f"Workspace introuvable : {workspace}")
        now = datetime.now(UTC)
        next_run = self.next_fire(payload.schedule)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_jobs SET name=?, schedule=?, prompt=?, workspace=?,
                   agent_id=?, skills_json=?, security_mode=?, provider_id=?, model=?,
                   reasoning=?, enabled=?, auto_resume=?, updated_at=?, next_run_at=?,
                   notification_session_id=?, workflow_json=?,
                   workflow_revision=workflow_revision+1, workflow_basis_hash=?,
                   workflow_updated_at=?
                   WHERE id=? AND in_flight=0""",
                (
                    payload.name.strip(),
                    payload.schedule.strip(),
                    payload.prompt,
                    str(workspace) if workspace is not None else "",
                    payload.agent_id,
                    json.dumps(payload.skills),
                    payload.security_mode.value,
                    payload.provider_id,
                    payload.model,
                    payload.reasoning,
                    int(payload.enabled),
                    int(payload.auto_resume),
                    now.isoformat(),
                    next_run.isoformat(),
                    str(payload.notification_session_id),
                    json.dumps(workflow, ensure_ascii=False),
                    workflow_basis_hash,
                    now.isoformat(),
                    job_id,
                ),
            )
        if not cursor.rowcount:
            raise SchedulerError("Impossible de modifier le workflow pendant une exécution")
        return self.get(job_id)

    def delete(self, job_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
        if cursor.rowcount == 0:
            raise SchedulerError(f"Cronjob introuvable : {job_id}")

    def set_workflow(
        self,
        job_id: str,
        workflow: dict[str, Any],
        workflow_basis_hash: str,
    ) -> CronJob:
        job = self.get(job_id)
        if job.in_flight:
            raise SchedulerError("Impossible de modifier le workflow pendant une exécution")
        now = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_jobs SET workflow_json=?,
                   workflow_revision=workflow_revision+1, workflow_basis_hash=?,
                   workflow_updated_at=?, updated_at=?
                   WHERE id=? AND in_flight=0""",
                (
                    json.dumps(workflow, ensure_ascii=False),
                    workflow_basis_hash,
                    now.isoformat(),
                    now.isoformat(),
                    job_id,
                ),
            )
        if not cursor.rowcount:
            raise SchedulerError("Impossible de modifier le workflow pendant une exécution")
        return self.get(job_id)

    def clear_workflow(self, job_id: str) -> CronJob:
        job = self.get(job_id)
        if job.in_flight:
            raise SchedulerError("Impossible de supprimer le workflow pendant une exécution")
        now = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_jobs SET workflow_json=NULL,
                   workflow_revision=workflow_revision+1, workflow_basis_hash=NULL,
                   workflow_updated_at=?, updated_at=?
                   WHERE id=? AND in_flight=0""",
                (now.isoformat(), now.isoformat(), job_id),
            )
        if not cursor.rowcount:
            raise SchedulerError("Impossible de supprimer le workflow pendant une exécution")
        return self.get(job_id)

    def due(self, now: datetime | None = None) -> list[CronJob]:
        current = now or datetime.now(UTC)
        return [
            job
            for job in self.list()
            if job.enabled
            and not job.in_flight
            and not job.blocked
            and job.next_run_at is not None
            and job.next_run_at.astimezone(UTC) <= current.astimezone(UTC)
        ]

    def claim(
        self,
        job_id: str,
        now: datetime | None = None,
        *,
        scheduled_for: datetime | None = None,
    ) -> CronJob | None:
        fired_at = now or datetime.now(UTC)
        job = self.get(job_id)
        occurrence_at = scheduled_for or fired_at
        occurrence_id = f"cronrun_{uuid4().hex}"
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_jobs SET in_flight=1, last_run_at=?, next_run_at=?,
                   last_error=NULL WHERE id=? AND enabled=1 AND in_flight=0 AND blocked=0""",
                (
                    fired_at.isoformat(),
                    self.next_fire(job.schedule, fired_at).isoformat(),
                    job_id,
                ),
            )
            if not cursor.rowcount:
                return None
            try:
                connection.execute(
                    """INSERT INTO cron_runs(
                        id, cron_job_id, scheduled_for, claimed_at, session_id,
                        notification_session_id, execution_status, delivery_status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'claimed', 'unread')""",
                    (
                        occurrence_id,
                        job_id,
                        occurrence_at.astimezone(UTC).isoformat(),
                        fired_at.astimezone(UTC).isoformat(),
                        str(job.session_id),
                        str(job.notification_session_id),
                    ),
                )
            except sqlite3.IntegrityError:
                connection.execute(
                    "UPDATE cron_jobs SET in_flight=0 WHERE id=?",
                    (job_id,),
                )
                return None
        return self.get(job_id).model_copy(
            update={"occurrence_id": occurrence_id, "scheduled_for": occurrence_at}
        )

    def mark_started(self, occurrence_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE cron_runs SET execution_status='running', started_at=?
                   WHERE id=?""",
                (datetime.now(UTC).isoformat(), occurrence_id),
            )

    def finish(
        self,
        job_id: str,
        result: RunResult | None,
        error: str | None = None,
        *,
        occurrence_id: str | None = None,
    ) -> None:
        status = result.status.value if result else "failed"
        message = error
        retryable = False
        if result and result.errors:
            message = result.errors[-1].message
            retryable = any(item.retryable for item in result.errors)
        if result and result.status in {RunStatus.TIMEOUT, RunStatus.PARTIAL}:
            retryable = True
        blocked = bool(result and result.status == RunStatus.APPROVAL_PENDING)
        with self._connect() as connection:
            connection.execute(
                """UPDATE cron_jobs SET in_flight=0, last_status=?, last_error=?,
                   last_retryable=?, blocked=? WHERE id=?""",
                (
                    status,
                    message[:500] if message else None,
                    int(retryable),
                    int(blocked),
                    job_id,
                ),
            )
            if occurrence_id:
                output = result.output if result else None
                connection.execute(
                    """UPDATE cron_runs SET completed_at=?, run_id=?,
                       execution_status=?, task_status=?, output_preview=?, error=?
                       WHERE id=?""",
                    (
                        datetime.now(UTC).isoformat(),
                        str(result.run_id) if result else None,
                        "blocked" if blocked else status,
                        status,
                        output[:1000] if output else None,
                        message[:1000] if message else None,
                        occurrence_id,
                    ),
                )

    def record_test_result(self, job_id: str, result: RunResult) -> None:
        """Store test diagnostics without changing scheduler blocking state."""
        message = result.errors[-1].message if result.errors else None
        retryable = any(item.retryable for item in result.errors)
        if result.status in {RunStatus.TIMEOUT, RunStatus.PARTIAL}:
            retryable = True
        with self._connect() as connection:
            connection.execute(
                """UPDATE cron_jobs SET last_status=?, last_error=?, last_retryable=?
                   WHERE id=?""",
                (
                    result.status.value,
                    message[:500] if message else None,
                    int(retryable),
                    job_id,
                ),
            )

    def list_runs(
        self,
        *,
        job_id: str | None = None,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[CronRun]:
        clauses: list[str] = []
        values: list[object] = []
        if job_id:
            clauses.append("cron_job_id=?")
            values.append(job_id)
        if unread_only:
            clauses.append("delivery_status='unread'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM cron_runs {where}
                    ORDER BY claimed_at DESC LIMIT ?""",
                values,
            ).fetchall()
        result: list[CronRun] = []
        for row in rows:
            payload = dict(row)
            # Defensive compatibility for a database opened before the
            # idempotent backfill above committed in another connection.
            payload["notification_session_id"] = payload.get("notification_session_id") or str(
                ROUTINE_INBOX_SESSION_ID
            )
            result.append(CronRun.model_validate(payload))
        return result

    def mark_run_read(self, occurrence_id: str) -> CronRun:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_runs SET delivery_status='read' WHERE id=?""",
                (occurrence_id,),
            )
        if not cursor.rowcount:
            raise SchedulerError(f"Occurrence cron introuvable : {occurrence_id}")
        return next(item for item in self.list_runs(limit=500) if item.id == occurrence_id)

    def mark_session_runs_read(self, session_id: UUID) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_runs SET delivery_status='read'
                   WHERE notification_session_id=? AND delivery_status='unread'""",
                (str(session_id),),
            )
        return cursor.rowcount

    def resume_for_session(
        self,
        session_id: UUID,
        result: RunResult,
        *,
        cron_job_id: str | None = None,
    ) -> None:
        """Complete the blocked occurrence after its approval resumes."""
        if result.status == RunStatus.APPROVAL_PENDING:
            return
        clauses = ["session_id=?", "run_id=?", "execution_status='blocked'"]
        values: list[object] = [str(session_id), str(result.run_id)]
        if cron_job_id:
            clauses.append("cron_job_id=?")
            values.append(cron_job_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT id, cron_job_id FROM cron_runs
                    WHERE {" AND ".join(clauses)} ORDER BY claimed_at DESC""",
                values,
            ).fetchall()
        for row in rows:
            self.finish(row["cron_job_id"], result, occurrence_id=row["id"])

    def resume_occurrence(self, occurrence_id: str, result: RunResult) -> None:
        """Complete the exact occurrence across chained approval run ids."""
        if result.status == RunStatus.APPROVAL_PENDING:
            return
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, cron_job_id FROM cron_runs
                   WHERE id=? AND execution_status='blocked'""",
                (occurrence_id,),
            ).fetchone()
        if row is not None:
            self.finish(row["cron_job_id"], result, occurrence_id=row["id"])

    def repair_orphaned_blocks(self, pending_session_ids: set[UUID]) -> int:
        """Unblock legacy occurrences whose approval batch no longer exists."""
        pending = {str(item) for item in pending_session_ids}
        repaired = 0
        with self._connect() as connection:
            jobs = connection.execute(
                """SELECT id, session_id, last_status FROM cron_jobs
                   WHERE blocked=1"""
            ).fetchall()
            for job in jobs:
                if job["session_id"] in pending:
                    continue
                terminal = (
                    job["last_status"]
                    if job["last_status"]
                    in {"success", "failed", "partial", "timeout", "cancelled"}
                    else "cancelled"
                )
                now = datetime.now(UTC).isoformat()
                connection.execute(
                    "UPDATE cron_jobs SET blocked=0 WHERE id=?",
                    (job["id"],),
                )
                connection.execute(
                    """UPDATE cron_runs SET execution_status=?, task_status=?,
                       completed_at=COALESCE(completed_at, ?)
                       WHERE cron_job_id=? AND execution_status='blocked'""",
                    (terminal, terminal, now, job["id"]),
                )
                repaired += 1
        return repaired

    def release_stale_claims(self) -> None:
        # A server restart means no task from the previous process is alive.
        with self._connect() as connection:
            connection.execute("UPDATE cron_jobs SET in_flight=0 WHERE in_flight=1")

    def record_heartbeat(self, now: datetime | None = None) -> None:
        value = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO scheduler_meta(key, value) VALUES('last_tick_at', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (value,),
            )

    def scheduler_status(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM scheduler_meta WHERE key='last_tick_at'"
            ).fetchone()
        last_tick = datetime.fromisoformat(row["value"]) if row else None
        age = (datetime.now(UTC) - last_tick).total_seconds() if last_tick else None
        return {
            "running": age is not None and age <= 60,
            "last_tick_at": last_tick.isoformat() if last_tick else None,
            "last_tick_age_seconds": age,
            "due_count": len(self.due()),
        }

    @staticmethod
    def _row(row: sqlite3.Row) -> CronJob:
        def parsed(name: str) -> datetime | None:
            return datetime.fromisoformat(row[name]) if row[name] else None

        return CronJob(
            id=row["id"],
            name=row["name"],
            schedule=row["schedule"],
            prompt=row["prompt"],
            workspace=Path(row["workspace"]) if row["workspace"] else None,
            agent_id=row["agent_id"],
            skills=json.loads(row["skills_json"]),
            security_mode=row["security_mode"],
            provider_id=row["provider_id"],
            model=row["model"],
            reasoning=row["reasoning"],
            enabled=bool(row["enabled"]),
            auto_resume=bool(row["auto_resume"]),
            notification_session_id=UUID(
                row["notification_session_id"] or str(ROUTINE_INBOX_SESSION_ID)
            ),
            session_id=UUID(row["session_id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            next_run_at=parsed("next_run_at"),
            last_run_at=parsed("last_run_at"),
            last_status=row["last_status"],
            last_error=row["last_error"],
            last_retryable=bool(row["last_retryable"]),
            in_flight=bool(row["in_flight"]),
            blocked=bool(row["blocked"]),
            workflow=json.loads(row["workflow_json"]) if row["workflow_json"] else None,
            workflow_revision=int(row["workflow_revision"] or 0),
            workflow_basis_hash=row["workflow_basis_hash"],
            workflow_updated_at=parsed("workflow_updated_at"),
        )


class CronScheduler:
    def __init__(
        self,
        service: CronService,
        launch: Callable[[RunRequest], Awaitable[RunResult]],
        *,
        tick_seconds: float = 15,
    ) -> None:
        self.service = service
        self.launch = launch
        self.tick_seconds = tick_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    async def run_forever(self) -> None:
        self.service.release_stale_claims()
        while True:
            await self.tick()
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, now: datetime | None = None) -> list[str]:
        self.service.record_heartbeat(now)
        launched: list[str] = []
        for due in self.service.due(now):
            job = self.service.claim(due.id, now, scheduled_for=due.next_run_at)
            if job is None:
                continue
            launched.append(job.id)
            task = asyncio.create_task(self._execute(job), name=f"amk-cron-{job.id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return launched

    @staticmethod
    def request_for(
        job: CronJob,
        *,
        trigger: Literal["cron", "cron_resume", "cron_test"] = "cron",
    ) -> RunRequest:
        resume = trigger == "cron_resume"
        prompt = (
            "Reprends la tâche interrompue à partir des traces et résultats déjà "
            "persistés. Ne rejoue pas les outils déjà terminés; vérifie l'état "
            "courant puis poursuis par la prochaine action utile."
            if resume
            else job.prompt
        )
        return RunRequest(
            prompt=prompt,
            agent_id=job.agent_id,
            skills=job.skills,
            session_id=job.session_id,
            workspace=job.workspace,
            security_mode=job.security_mode,
            provider_id=job.provider_id,
            model=job.model,
            reasoning=job.reasoning,
            trigger=trigger,
            cron_job_id=job.id,
            cron_occurrence_id=job.occurrence_id,
            workflow=job.workflow,
            tool_allowlist=_workflow_tool_allowlist(job.workflow),
        )

    async def _execute(self, job: CronJob) -> None:
        resume = job.auto_resume and job.last_retryable
        request = self.request_for(job, trigger="cron_resume" if resume else "cron")
        try:
            if job.occurrence_id:
                self.service.mark_started(job.occurrence_id)
            self.service.finish(
                job.id,
                await self.launch(request),
                occurrence_id=job.occurrence_id,
            )
        except asyncio.CancelledError:
            self.service.finish(
                job.id,
                None,
                "scheduler stopped",
                occurrence_id=job.occurrence_id,
            )
            raise
        except Exception as exc:
            self.service.finish(
                job.id,
                None,
                str(exc),
                occurrence_id=job.occurrence_id,
            )


def _workflow_tool_allowlist(workflow: dict[str, Any] | None) -> list[str] | None:
    """Return the exact tool set declared by an accepted workflow.

    ``None`` is intentionally distinct from ``[]``: no workflow preserves the
    historical free selection, while a synthesis-only workflow exposes no
    module tools.
    """
    if workflow is None:
        return None
    definition = WorkflowDefinition.model_validate(workflow)
    if definition.status != "ready":
        return []
    return workflow_tool_allowlist(definition)
