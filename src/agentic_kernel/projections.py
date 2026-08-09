from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from .models import Event

TERMINAL_STATUSES = {"success", "failed", "partial", "timeout", "cancelled"}


class SessionProjection:
    """Rebuildable SQLite projection over append-only session JSONL files."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self._initialize()

    def _db(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projected_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source_offset INTEGER NOT NULL,
                    source_length INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    UNIQUE(session_id, source_offset)
                );
                CREATE INDEX IF NOT EXISTS projected_events_session_sequence
                    ON projected_events(session_id, sequence);
                CREATE TABLE IF NOT EXISTS projected_sessions (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    workspace TEXT,
                    trigger TEXT NOT NULL DEFAULT 'user',
                    hidden INTEGER NOT NULL DEFAULT 0,
                    cron_job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    output TEXT,
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    event_count INTEGER NOT NULL DEFAULT 0,
                    last_sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS projected_sessions_workspace_updated
                    ON projected_sessions(workspace, updated_at DESC);
                CREATE TABLE IF NOT EXISTS projected_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    terminal_status TEXT
                );
                CREATE TABLE IF NOT EXISTS projected_run_transitions (
                    sequence INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projected_context (
                    session_id TEXT PRIMARY KEY,
                    estimated_history_tokens INTEGER NOT NULL DEFAULT 0,
                    observed_input_tokens INTEGER,
                    compaction_count INTEGER NOT NULL DEFAULT 0,
                    calibration_factor REAL NOT NULL DEFAULT 1.0,
                    calibration_samples INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projected_messages (
                    sequence INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS projected_messages_session_sequence
                    ON projected_messages(session_id, sequence);
                CREATE TABLE IF NOT EXISTS projected_artifacts (
                    sequence INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS projected_artifacts_session_run
                    ON projected_artifacts(session_id, run_id);
                CREATE TABLE IF NOT EXISTS projection_offsets (
                    session_id TEXT PRIMARY KEY,
                    source_offset INTEGER NOT NULL
                );
                """
            )
            # L'ancienne boîte unique des routines n'a plus de gestionnaire
            # d'événement : une reprojection complète ne la recréerait pas. La
            # retirer ici met l'état incrémental d'accord avec cette reprojection,
            # au lieu de laisser un fil qu'aucun écran ne montre mais où des
            # résultats continuaient d'être écrits. Les exécutions elles-mêmes
            # restent dans `cron_runs`, donc rien de consultable n'est perdu.
            db.execute("DELETE FROM projected_sessions WHERE trigger='routine_inbox'")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(projected_context)")}
            if "calibration_factor" not in columns:
                db.execute(
                    "ALTER TABLE projected_context "
                    "ADD COLUMN calibration_factor REAL NOT NULL DEFAULT 1.0"
                )
            if "calibration_samples" not in columns:
                db.execute(
                    "ALTER TABLE projected_context "
                    "ADD COLUMN calibration_samples INTEGER NOT NULL DEFAULT 0"
                )
            session_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(projected_sessions)")
            }
            if "hidden" not in session_columns:
                db.execute(
                    "ALTER TABLE projected_sessions "
                    "ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
                )

    def apply(
        self,
        event: Event,
        *,
        source_offset: int,
        source_length: int,
    ) -> int:
        session_id = str(event.session_id)
        run_id = str(event.run_id)
        timestamp = event.timestamp.isoformat()
        with self._db() as db:
            existing = db.execute(
                """SELECT sequence FROM projected_events
                   WHERE session_id = ? AND source_offset = ?""",
                (session_id, source_offset),
            ).fetchone()
            if existing:
                return int(existing["sequence"])
            cursor = db.execute(
                """INSERT INTO projected_events
                   (session_id, run_id, event_type, source_offset, source_length, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    run_id,
                    event.type,
                    source_offset,
                    source_length,
                    timestamp,
                ),
            )
            sequence = int(cursor.lastrowid)
            self._project_event(db, event, sequence)
            db.execute(
                """INSERT INTO projection_offsets(session_id, source_offset)
                   VALUES (?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                   source_offset = max(source_offset, excluded.source_offset)""",
                (session_id, source_offset + source_length),
            )
            return sequence

    def _project_event(  # noqa: C901 - dette: projection par type d'événement
        self,
        db: sqlite3.Connection,
        event: Event,
        sequence: int,
    ) -> None:
        session_id = str(event.session_id)
        run_id = str(event.run_id)
        timestamp = event.timestamp.isoformat()
        if event.type == "agent.channel.created":
            payload = event.payload
            db.execute(
                """INSERT INTO projected_sessions
                   (session_id, agent_id, prompt, workspace, trigger, hidden, cron_job_id,
                    created_at, updated_at, status, output, errors_json,
                    event_count, last_sequence)
                   VALUES (?, ?, ?, NULL, 'agent_channel', 0, NULL, ?, ?, 'success',
                           '', '[]', 1, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     prompt=excluded.prompt, workspace=NULL, trigger='agent_channel',
                     cron_job_id=NULL""",
                (session_id, event.agent_id, str(payload.get("prompt") or event.agent_id),
                 timestamp, timestamp, sequence),
            )
            return
        if event.type == "session.cleared":
            # Vider une session n'est ni la démarrer ni créer une boîte de
            # routines : le statut reste terminal, et le trigger, le workspace
            # et la visibilité d'origine sont restitués depuis le payload —
            # `reset` a effacé le journal, plus rien d'autre ne les porte.
            payload = event.payload
            db.execute(
                """INSERT INTO projected_sessions
                   (session_id, agent_id, prompt, workspace, trigger, hidden, cron_job_id,
                    created_at, updated_at, status, output, errors_json,
                    event_count, last_sequence)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'success', '', '[]', 1, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     prompt=excluded.prompt,
                     workspace=excluded.workspace,
                     trigger=excluded.trigger,
                     hidden=excluded.hidden,
                     cron_job_id=NULL,
                     updated_at=excluded.updated_at,
                     status='success', output='', errors_json='[]',
                     event_count=1, last_sequence=excluded.last_sequence""",
                (
                    session_id,
                    event.agent_id,
                    str(payload.get("prompt", "")),
                    payload.get("workspace"),
                    str(payload.get("trigger", "user")),
                    int(bool(payload.get("hidden", False))),
                    timestamp,
                    timestamp,
                    sequence,
                ),
            )
            return
        if event.type == "session.started":
            payload = event.payload
            db.execute(
                """INSERT INTO projected_sessions
                   (session_id, agent_id, prompt, workspace, trigger, hidden, cron_job_id,
                    created_at, updated_at, status, event_count, last_sequence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 1, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     agent_id=excluded.agent_id,
                     prompt=CASE WHEN projected_sessions.trigger='agent_channel'
                       THEN projected_sessions.prompt ELSE excluded.prompt END,
                     workspace=CASE WHEN projected_sessions.trigger='agent_channel'
                       THEN NULL ELSE excluded.workspace END,
                     trigger=CASE WHEN projected_sessions.trigger='agent_channel'
                       THEN 'agent_channel' ELSE excluded.trigger END,
                     hidden=CASE WHEN projected_sessions.trigger='agent_channel'
                       THEN 0 ELSE excluded.hidden END,
                     cron_job_id=CASE WHEN projected_sessions.trigger='agent_channel'
                       THEN NULL ELSE excluded.cron_job_id END,
                     updated_at=excluded.updated_at,
                     status='running', event_count=event_count + 1,
                     last_sequence=excluded.last_sequence""",
                (
                    session_id,
                    event.agent_id,
                    str(payload.get("prompt", "")),
                    payload.get("workspace"),
                    str(payload.get("trigger", "user")),
                    int(bool(payload.get("hidden", False))),
                    payload.get("cron_job_id"),
                    timestamp,
                    timestamp,
                    sequence,
                ),
            )
            db.execute(
                """INSERT OR REPLACE INTO projected_runs
                   (run_id, session_id, agent_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'created', ?, ?)""",
                (run_id, session_id, event.agent_id, timestamp, timestamp),
            )
            self._transition(db, sequence, run_id, "created", timestamp)
            db.execute(
                """INSERT OR REPLACE INTO projected_messages
                   (sequence, session_id, run_id, role, content, status, timestamp)
                   VALUES (?, ?, ?, 'user', ?, NULL, ?)""",
                (
                    sequence,
                    session_id,
                    run_id,
                    str(payload.get("prompt", "")),
                    timestamp,
                ),
            )
        else:
            db.execute(
                """UPDATE projected_sessions
                   SET updated_at=?, event_count=event_count + 1, last_sequence=?
                   WHERE session_id=?""",
                (timestamp, sequence, session_id),
            )

        if event.type == "routine.notification":
            content = str(event.payload.get("content") or "")
            status = str(event.payload.get("status") or "success")
            db.execute(
                """UPDATE projected_sessions SET status=?, output=?
                   WHERE session_id=?""",
                (status, content, session_id),
            )
            db.execute(
                """INSERT OR REPLACE INTO projected_messages
                   (sequence, session_id, run_id, role, content, status, timestamp)
                   VALUES (?, ?, ?, 'assistant', ?, ?, ?)""",
                (sequence, session_id, run_id, content, status, timestamp),
            )

        state = None
        if event.type == "run.suspended":
            state = "approval_pending"
        elif event.type == "run.resumed":
            state = "resuming"
        elif event.type == "run.transitioned":
            state = str(event.payload.get("state", "running"))
        if state:
            self._set_run_state(db, run_id, state, timestamp, sequence)
            db.execute(
                "UPDATE projected_sessions SET status=? WHERE session_id=?",
                (state, session_id),
            )

        if event.type == "session.completed":
            status = str(event.payload.get("status", "failed"))
            # Legacy journals used session.completed for a suspension. Preserve
            # compatibility without treating it as a terminal transition.
            if status == "approval_pending":
                self._set_run_state(
                    db,
                    run_id,
                    "approval_pending",
                    timestamp,
                    sequence,
                )
            else:
                output = str(event.payload.get("output") or "")
                db.execute(
                    """UPDATE projected_sessions
                       SET status=?, output=?, errors_json=? WHERE session_id=?""",
                    (
                        status,
                        output,
                        json.dumps(event.payload.get("errors", [])),
                        session_id,
                    ),
                )
                terminal = status if status in TERMINAL_STATUSES else "failed"
                self._set_run_state(
                    db,
                    run_id,
                    terminal,
                    timestamp,
                    sequence,
                    terminal=terminal,
                )
                db.execute(
                    """INSERT OR REPLACE INTO projected_messages
                       (sequence, session_id, run_id, role, content, status, timestamp)
                       VALUES (?, ?, ?, 'assistant', ?, ?, ?)""",
                    (sequence, session_id, run_id, output, terminal, timestamp),
                )
                usage = event.payload.get("usage", {})
                observed = usage.get("input_tokens") if isinstance(usage, dict) else None
                if isinstance(observed, int):
                    self._upsert_context(db, session_id, timestamp, observed=observed)
                    self._calibrate_context(db, session_id, observed)

        if event.type == "messages.snapshot":
            messages = event.payload.get("messages")
            if isinstance(messages, list):
                encoded = json.dumps(messages, ensure_ascii=False)
                estimate = max(1, math.ceil(len(encoded.encode("utf-8")) / 3.5))
                self._upsert_context(db, session_id, timestamp, estimated=estimate)
            elif isinstance(event.payload.get("estimated_tokens"), int):
                self._upsert_context(
                    db,
                    session_id,
                    timestamp,
                    estimated=int(event.payload["estimated_tokens"]),
                )
        elif event.type == "context.compacted":
            self._upsert_context(db, session_id, timestamp, increment_compaction=True)
        if event.type == "artifact.created":
            db.execute(
                """INSERT OR REPLACE INTO projected_artifacts
                   (sequence, session_id, run_id, payload_json, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    sequence,
                    session_id,
                    run_id,
                    json.dumps(event.payload, ensure_ascii=False),
                    timestamp,
                ),
            )

    @staticmethod
    def _transition(
        db: sqlite3.Connection,
        sequence: int,
        run_id: str,
        state: str,
        timestamp: str,
    ) -> None:
        db.execute(
            """INSERT OR REPLACE INTO projected_run_transitions
               (sequence, run_id, state, timestamp) VALUES (?, ?, ?, ?)""",
            (sequence, run_id, state, timestamp),
        )

    def _set_run_state(
        self,
        db: sqlite3.Connection,
        run_id: str,
        state: str,
        timestamp: str,
        sequence: int,
        *,
        terminal: str | None = None,
    ) -> None:
        db.execute(
            """UPDATE projected_runs
               SET state=?, updated_at=?, terminal_status=coalesce(?, terminal_status)
               WHERE run_id=?""",
            (state, timestamp, terminal, run_id),
        )
        self._transition(db, sequence, run_id, state, timestamp)

    @staticmethod
    def _upsert_context(
        db: sqlite3.Connection,
        session_id: str,
        timestamp: str,
        *,
        estimated: int | None = None,
        observed: int | None = None,
        increment_compaction: bool = False,
    ) -> None:
        db.execute(
            """INSERT INTO projected_context
               (session_id, estimated_history_tokens, observed_input_tokens,
                compaction_count, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 estimated_history_tokens=coalesce(?, estimated_history_tokens),
                 observed_input_tokens=coalesce(?, observed_input_tokens),
                 compaction_count=compaction_count + ?,
                 updated_at=excluded.updated_at""",
            (
                session_id,
                estimated or 0,
                observed,
                int(increment_compaction),
                timestamp,
                estimated,
                observed,
                int(increment_compaction),
            ),
        )

    @staticmethod
    def _calibrate_context(
        db: sqlite3.Connection,
        session_id: str,
        observed: int,
    ) -> None:
        row = db.execute(
            """SELECT estimated_history_tokens, calibration_factor,
                      calibration_samples
               FROM projected_context WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        if row is None or int(row["estimated_history_tokens"]) <= 0:
            return
        # Le plafond borne une mesure aberrante, pas la réalité : un écart de
        # 2,4 a été observé sur une session de code, et le plafond précédent de
        # 2.0 le tronquait — la correction restait insuffisante là où elle était
        # le plus nécessaire.
        sample = max(
            0.5,
            min(4.0, observed / int(row["estimated_history_tokens"])),
        )
        samples = int(row["calibration_samples"])
        factor = (float(row["calibration_factor"]) * samples + sample) / (samples + 1)
        db.execute(
            """UPDATE projected_context
               SET calibration_factor=?, calibration_samples=?
               WHERE session_id=?""",
            (factor, samples + 1, session_id),
        )

    def list_sessions(
        self,
        workspace: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        include_automations: bool = False,
        include_channels: bool = False,
        default_workspace_agent: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM projected_sessions"
        params: list[Any] = []
        clauses: list[str] = []
        if workspace is not None:
            workspace_clauses = ["workspace = ?", "trigger = 'agent_channel'"]
            params.append(workspace)
            if default_workspace_agent:
                workspace_clauses.append("(workspace IS NULL AND agent_id = ?)")
                params.append(default_workspace_agent)
            if include_channels:
                workspace_clauses.append("trigger = 'telegram'")
            clauses.append("(" + " OR ".join(workspace_clauses) + ")")
        if not include_automations:
            clauses.append("trigger NOT IN ('cron', 'cron_resume', 'cron_test')")
        clauses.append("hidden = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._db() as db:
            rows = db.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "errors": json.loads(row["errors_json"]),
            }
            for row in rows
        ]

    def context(self, session_id: UUID) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM projected_context WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        return dict(row) if row else None

    def session(self, session_id: UUID) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM projected_sessions WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "errors": json.loads(row["errors_json"])}

    def messages(
        self,
        session_id: UUID,
        *,
        limit: int = 100,
        before_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM projected_messages WHERE session_id=?"
        params: list[Any] = [str(session_id)]
        if before_sequence is not None:
            query += " AND sequence < ?"
            params.append(before_sequence)
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        with self._db() as db:
            rows = db.execute(query, params).fetchall()
        result = [dict(row) for row in reversed(rows)]
        if not result:
            return result
        run_ids = sorted({row["run_id"] for row in result})
        placeholders = ",".join("?" for _ in run_ids)
        with self._db() as db:
            artifact_rows = db.execute(
                f"""SELECT run_id, payload_json FROM projected_artifacts
                    WHERE session_id=? AND run_id IN ({placeholders})
                    ORDER BY sequence""",  # noqa: S608 - placeholders ?, valeurs paramétrées
                [str(session_id), *run_ids],
            ).fetchall()
        artifacts: dict[str, list[dict[str, Any]]] = {}
        for row in artifact_rows:
            artifacts.setdefault(row["run_id"], []).append(json.loads(row["payload_json"]))
        for message in result:
            message["artifacts"] = artifacts.get(message["run_id"], [])
        return result

    def event_positions(
        self,
        session_id: UUID,
        *,
        limit: int = 200,
        before_sequence: int | None = None,
    ) -> list[dict[str, int]]:
        query = """SELECT sequence, source_offset, source_length
                   FROM projected_events WHERE session_id=?"""
        params: list[Any] = [str(session_id)]
        if before_sequence is not None:
            query += " AND sequence < ?"
            params.append(before_sequence)
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        with self._db() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def needs_message_backfill(self, session_id: UUID) -> bool:
        with self._db() as db:
            event_count = db.execute(
                "SELECT count(*) AS count FROM projected_events WHERE session_id=?",
                (str(session_id),),
            ).fetchone()["count"]
            message_count = db.execute(
                "SELECT count(*) AS count FROM projected_messages WHERE session_id=?",
                (str(session_id),),
            ).fetchone()["count"]
            artifact_event_count = db.execute(
                """SELECT count(*) AS count FROM projected_events
                   WHERE session_id=? AND event_type='artifact.created'""",
                (str(session_id),),
            ).fetchone()["count"]
            artifact_count = db.execute(
                "SELECT count(*) AS count FROM projected_artifacts WHERE session_id=?",
                (str(session_id),),
            ).fetchone()["count"]
        return bool((event_count and not message_count) or artifact_event_count != artifact_count)

    def backfill_messages(self, session_id: UUID, events: list[Event]) -> None:
        """One-time idempotent migration for journals indexed before this projection."""
        rows: list[tuple[int, str, str, str, str, str | None, str]] = []
        with self._db() as db:
            sequences = {
                (row["run_id"], row["event_type"], row["timestamp"]): row["sequence"]
                for row in db.execute(
                    """SELECT sequence, run_id, event_type, timestamp
                       FROM projected_events WHERE session_id=?""",
                    (str(session_id),),
                )
            }
            for event in events:
                sequence = sequences.get(
                    (str(event.run_id), event.type, event.timestamp.isoformat())
                )
                if sequence is None:
                    continue
                if event.type == "session.started":
                    rows.append(
                        (
                            sequence,
                            str(session_id),
                            str(event.run_id),
                            "user",
                            str(event.payload.get("prompt", "")),
                            None,
                            event.timestamp.isoformat(),
                        )
                    )
                elif event.type == "session.completed":
                    status = str(event.payload.get("status", "failed"))
                    if status == "approval_pending":
                        continue
                    rows.append(
                        (
                            sequence,
                            str(session_id),
                            str(event.run_id),
                            "assistant",
                            str(event.payload.get("output") or ""),
                            status,
                            event.timestamp.isoformat(),
                        )
                    )
                elif event.type == "artifact.created":
                    db.execute(
                        """INSERT OR REPLACE INTO projected_artifacts
                           (sequence, session_id, run_id, payload_json, timestamp)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            sequence,
                            str(session_id),
                            str(event.run_id),
                            json.dumps(event.payload, ensure_ascii=False),
                            event.timestamp.isoformat(),
                        ),
                    )
            db.executemany(
                """INSERT OR REPLACE INTO projected_messages
                   (sequence, session_id, run_id, role, content, status, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def positions_after(
        self,
        session_id: UUID,
        sequence: int,
        *,
        limit: int = 500,
    ) -> list[dict[str, int]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT sequence, source_offset, source_length
                   FROM projected_events
                   WHERE session_id=? AND sequence>?
                   ORDER BY sequence LIMIT ?""",
                (str(session_id), sequence, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_offset(self, session_id: UUID) -> int:
        with self._db() as db:
            row = db.execute(
                "SELECT source_offset FROM projection_offsets WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        return int(row["source_offset"]) if row else 0

    def delete_session(self, session_id: UUID) -> None:
        with self._db() as db:
            self._delete_session(db, session_id)

    def reset(self, event: Event, *, source_length: int) -> None:
        """Replace one session projection with its seed in one transaction."""
        session_id = str(event.session_id)
        with self._db() as db:
            self._delete_session(db, event.session_id)
            cursor = db.execute(
                """INSERT INTO projected_events
                   (session_id, run_id, event_type, source_offset, source_length, timestamp)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (
                    session_id,
                    str(event.run_id),
                    event.type,
                    source_length,
                    event.timestamp.isoformat(),
                ),
            )
            self._project_event(db, event, int(cursor.lastrowid))
            db.execute(
                "INSERT INTO projection_offsets(session_id, source_offset) VALUES (?, ?)",
                (session_id, source_length),
            )

    @staticmethod
    def _delete_session(db: sqlite3.Connection, session_id: UUID) -> None:
        value = str(session_id)
        run_ids = [
            row["run_id"]
            for row in db.execute(
                "SELECT run_id FROM projected_runs WHERE session_id=?",
                (value,),
            )
        ]
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            db.execute(
                f"DELETE FROM projected_run_transitions WHERE run_id IN ({placeholders})",  # noqa: S608 - placeholders ?, valeurs paramétrées
                run_ids,
            )
        for table in (
            "projected_events",
            "projected_runs",
            "projected_context",
            "projected_messages",
            "projected_artifacts",
            "projection_offsets",
            "projected_sessions",
        ):
            db.execute(f"DELETE FROM {table} WHERE session_id=?", (value,))  # noqa: S608 - table issue d'un tuple constant
