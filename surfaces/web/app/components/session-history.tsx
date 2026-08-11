"use client";

import { Icon } from "../theme/theme-context";

/**
 * Dernier segment d'un chemin de projet, quel que soit le système.
 *
 * Le chemin complet ne tient pas dans le panneau et se ressemble d'une ligne à
 * l'autre — seul le nom du dossier distingue réellement deux conversations.
 */
function nomDeProjet(workspace?: string | null): string {
  if (!workspace) return "";
  const segments = workspace.replace(/[\\/]+$/, "").split(/[\\/]/);
  return segments[segments.length - 1] || "";
}

export type SessionHistoryItem = {
  session_id: string;
  prompt: string;
  updated_at: string;
  status: string;
  /** Nomme le canal permanent, qui appartient à un agent et non à un projet. */
  agent_id?: string;
  /** Projet rattaché; `null` pour l'espace personnel de l'agent. */
  workspace?: string | null;
  trigger?: "user" | "resume" | "cron" | "cron_resume" | "cron_test" | "agent_channel" | "telegram";
};

type SessionHistoryProps = {
  sessions: SessionHistoryItem[];
  /**
   * Agents du catalogue. Un canal reste permanent tant que son agent existe;
   * quand il a disparu, le canal ne sera pas recréé au démarrage et le cacher
   * le rendrait éternel — c'était le cas d'un agent de test resté en place.
   */
  knownAgents: string[];
  activeSessionId: string | null;
  unreadSessionIds: Set<string>;
  running: boolean;
  onOpen: (sessionId: string) => void;
  onResume: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
};

export function SessionHistory({
  sessions,
  knownAgents,
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
                    {/* Le projet distingue deux conversations qui commencent
                        par la même phrase — « /compact », « Continue. » — et
                        que rien d'autre ne séparait dans la liste. */}
                    {session.trigger !== "agent_channel" && nomDeProjet(session.workspace) && (
                      <em className="session-project">{nomDeProjet(session.workspace)}</em>
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
              {(session.trigger !== "agent_channel"
                || !knownAgents.includes(session.agent_id || "")) && (
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
