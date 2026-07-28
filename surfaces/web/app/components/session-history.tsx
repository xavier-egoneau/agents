"use client";

export type SessionHistoryItem = {
  session_id: string;
  prompt: string;
  updated_at: string;
  status: string;
  trigger?: "user" | "resume" | "cron" | "cron_resume" | "cron_test";
};

type SessionHistoryProps = {
  sessions: SessionHistoryItem[];
  activeSessionId: string | null;
  unreadSessionIds: Set<string>;
  running: boolean;
  onOpen: (sessionId: string) => void;
  onResume: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
};

export function SessionHistory({
  sessions,
  activeSessionId,
  unreadSessionIds,
  running,
  onOpen,
  onResume,
  onDelete,
}: SessionHistoryProps) {
  return (
    <section className="rail-section history-section">
      <div className="section-title">
        <p className="eyebrow">Historique</p>
        <span>{sessions.length}</span>
      </div>
      <div className="session-list">
        {sessions.map((session) => {
          const unread = unreadSessionIds.has(session.session_id);
          return (
            <div
              key={session.session_id}
              className={[
                "session-entry",
                session.session_id === activeSessionId ? "active" : "",
                unread ? "unread" : "",
              ].filter(Boolean).join(" ")}
            >
              <button
                className="session-open"
                onClick={() => onOpen(session.session_id)}
                title={session.prompt}
              >
                <span className={`session-state ${session.status}`} />
                <span>
                  <strong>
                    {session.prompt || "Session sans titre"}
                    {session.trigger?.startsWith("cron") && (
                      <em className="automation-chip">Routine</em>
                    )}
                  </strong>
                  <small>
                    {new Date(session.updated_at).toLocaleString("fr-FR", {
                      dateStyle: "short",
                      timeStyle: "short",
                    })}
                  </small>
                </span>
              </button>
              {unread && (
                <span
                  className="session-unread"
                  title="Nouveau résultat"
                  aria-label="Nouveau résultat"
                />
              )}
              <button
                className="session-resume"
                hidden={!["failed", "timeout", "partial", "cancelled"].includes(session.status)}
                onClick={() => onResume(session.session_id)}
                disabled={running}
                aria-label="Reprendre la session"
                title="Reprendre à partir des traces persistées"
              >↻</button>
              <button
                className="session-delete"
                onClick={() => onDelete(session.session_id)}
                aria-label="Supprimer la session"
                title="Supprimer la session"
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M5 7h14M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5" />
                </svg>
              </button>
            </div>
          );
        })}
        {sessions.length === 0 && <p className="empty-label">Aucune session</p>}
      </div>
    </section>
  );
}
