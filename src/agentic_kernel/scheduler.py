from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from .models import RunRequest, RunResult, RunStatus, SecurityMode


class CronJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    schedule: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1)
    workspace: Path
    agent_id: str = "main"
    skills: list[str] = Field(default_factory=list)
    security_mode: SecurityMode = SecurityMode.LIMITED
    provider_id: str | None = None
    model: str | None = None
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    enabled: bool = True
    auto_resume: bool = True


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
                    in_flight INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(cron_jobs)")}
            if "last_retryable" not in columns:
                connection.execute(
                    "ALTER TABLE cron_jobs ADD COLUMN last_retryable INTEGER NOT NULL DEFAULT 0"
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

    def create(self, payload: CronJobInput) -> CronJob:
        self.validate_schedule(payload.schedule)
        workspace = payload.workspace.expanduser().resolve()
        if not workspace.is_dir():
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
                    last_status, last_error, last_retryable, in_flight
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0
                )""",
                (
                    job_id,
                    payload.name.strip(),
                    payload.schedule.strip(),
                    payload.prompt,
                    str(workspace),
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
        workspace = payload.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise SchedulerError(f"Workspace introuvable : {workspace}")
        now = datetime.now(UTC)
        next_run = self.next_fire(payload.schedule)
        with self._connect() as connection:
            connection.execute(
                """UPDATE cron_jobs SET name=?, schedule=?, prompt=?, workspace=?,
                   agent_id=?, skills_json=?, security_mode=?, provider_id=?, model=?,
                   reasoning=?, enabled=?, auto_resume=?, updated_at=?, next_run_at=?
                   WHERE id=?""",
                (
                    payload.name.strip(),
                    payload.schedule.strip(),
                    payload.prompt,
                    str(workspace),
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
                    job_id,
                ),
            )
        return self.get(job_id)

    def delete(self, job_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
        if cursor.rowcount == 0:
            raise SchedulerError(f"Cronjob introuvable : {job_id}")

    def due(self, now: datetime | None = None) -> list[CronJob]:
        current = now or datetime.now(UTC)
        return [
            job
            for job in self.list()
            if job.enabled
            and not job.in_flight
            and job.next_run_at is not None
            and job.next_run_at.astimezone(UTC) <= current.astimezone(UTC)
        ]

    def claim(self, job_id: str, now: datetime | None = None) -> CronJob | None:
        fired_at = now or datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE cron_jobs SET in_flight=1, last_run_at=?, next_run_at=?,
                   last_error=NULL WHERE id=? AND enabled=1 AND in_flight=0""",
                (
                    fired_at.isoformat(),
                    self.next_fire(self.get(job_id).schedule, fired_at).isoformat(),
                    job_id,
                ),
            )
        return self.get(job_id) if cursor.rowcount else None

    def finish(self, job_id: str, result: RunResult | None, error: str | None = None) -> None:
        status = result.status.value if result else "failed"
        message = error
        retryable = False
        if result and result.errors:
            message = result.errors[-1].message
            retryable = any(item.retryable for item in result.errors)
        if result and result.status in {RunStatus.TIMEOUT, RunStatus.PARTIAL}:
            retryable = True
        with self._connect() as connection:
            connection.execute(
                """UPDATE cron_jobs SET in_flight=0, last_status=?, last_error=?,
                   last_retryable=? WHERE id=?""",
                (status, message[:500] if message else None, int(retryable), job_id),
            )

    def release_stale_claims(self) -> None:
        # A server restart means no task from the previous process is alive.
        with self._connect() as connection:
            connection.execute("UPDATE cron_jobs SET in_flight=0 WHERE in_flight=1")

    @staticmethod
    def _row(row: sqlite3.Row) -> CronJob:
        def parsed(name: str) -> datetime | None:
            return datetime.fromisoformat(row[name]) if row[name] else None

        return CronJob(
            id=row["id"],
            name=row["name"],
            schedule=row["schedule"],
            prompt=row["prompt"],
            workspace=Path(row["workspace"]),
            agent_id=row["agent_id"],
            skills=json.loads(row["skills_json"]),
            security_mode=row["security_mode"],
            provider_id=row["provider_id"],
            model=row["model"],
            reasoning=row["reasoning"],
            enabled=bool(row["enabled"]),
            auto_resume=bool(row["auto_resume"]),
            session_id=UUID(row["session_id"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            next_run_at=parsed("next_run_at"),
            last_run_at=parsed("last_run_at"),
            last_status=row["last_status"],
            last_error=row["last_error"],
            last_retryable=bool(row["last_retryable"]),
            in_flight=bool(row["in_flight"]),
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
        launched: list[str] = []
        for due in self.service.due(now):
            job = self.service.claim(due.id, now)
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
        )

    async def _execute(self, job: CronJob) -> None:
        resume = job.auto_resume and job.last_retryable
        request = self.request_for(job, trigger="cron_resume" if resume else "cron")
        try:
            self.service.finish(job.id, await self.launch(request))
        except asyncio.CancelledError:
            self.service.finish(job.id, None, "scheduler stopped")
            raise
        except Exception as exc:
            self.service.finish(job.id, None, str(exc))
