from __future__ import annotations

from uuid import UUID

from .events import JsonlEventStore
from .models import Event


def recover_stale_sessions(events: JsonlEventStore) -> None:
    """Close every session left open by a previous server shutdown.

    Without this recovery, a session whose run was in progress when the server
    stopped stays visible as ``running`` in the projection, and a resumed or
    new session can inadvertently process tool calls or pending approvals
    belonging to the dead run — the agent appears to "switch between sessions".
    """
    with events.projection._db() as db:
        rows = db.execute(
            "SELECT session_id FROM projected_sessions WHERE status='running'"
        ).fetchall()
    stale = [UUID(row["session_id"]) for row in rows]
    if not stale:
        return
    for session_id in stale:
        incomplete: set[UUID] = set()
        for event in events.read(session_id):
            if event.type == "session.started":
                incomplete.add(event.run_id)
            elif event.type == "session.completed":
                incomplete.discard(event.run_id)
        for run_id in incomplete:
            events.append(
                Event(
                    session_id=session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="session.completed",
                    payload={
                        "status": "interrupted",
                        "output": "Ce run a été interrompu par un redémarrage du serveur.",
                        "errors": [
                            {
                                "type": "interrupted",
                                "message": "Redémarrage du serveur pendant le run.",
                                "retryable": False,
                            }
                        ],
                    },
                )
            )
