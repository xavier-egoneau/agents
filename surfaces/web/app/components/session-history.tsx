"use client";

import { Icon } from "../theme/theme-context";

export type SessionHistoryItem = {
  session_id: string;
  prompt: string;
  updated_at: string;
  status: string;
  /** Nomme le canal permanent, qui appartient à un agent et non à un projet. */
  agent_id?: string;
  trigger?: "user" | "resume" | "cron" | "cron_resume" | "cron_test" | "agent_channel" | "telegram";
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
                    {session.trigger === "agent_channel"
                      ? `${session.agent_id} · canonique`
                      : session.trigger === "telegram"
                        ? `Telegram · ${session.prompt || "Conversation"}`
                        : session.prompt || "Session sans titre"}
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
              >
                <Icon name="refresh" size="xs" />
              </button>
              {session.trigger !== "agent_channel" && (
                <button
                  className="session-delete"
                  onClick={() => onDelete(session.session_id)}
                  aria-label="Supprimer la session"
                  title="Supprimer la session"
                >
                  <Icon name="remove" size="xs" />
                </button>
              )}
            </div>
          );
        })}
      {sessions.length === 0 && <p className="empty-label">Aucune session</p>}
    </div>
  );
}
