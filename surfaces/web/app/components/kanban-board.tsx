"use client";

import { useEffect, useState } from "react";

type Status = "backlog" | "ready" | "running" | "review" | "done" | "blocked";
type Priority = "low" | "medium" | "high" | "urgent";

export type KanbanTask = {
  id: string;
  workspace: string;
  title: string;
  prompt: string;
  acceptance_criteria: string;
  status: Status;
  priority: Priority;
  auto_run: boolean;
  agent_id: string;
  reasoning: string;
  last_error?: string | null;
};

const columns: Array<{ id: Status; label: string }> = [
  { id: "backlog", label: "Backlog" },
  { id: "ready", label: "Prêt" },
  { id: "running", label: "En cours" },
  { id: "review", label: "À vérifier" },
  { id: "done", label: "Terminé" },
  { id: "blocked", label: "Bloqué" },
];

const emptyDraft = (workspace: string): Partial<KanbanTask> => ({
  workspace,
  title: "",
  prompt: "",
  acceptance_criteria: "",
  status: "backlog",
  priority: "medium",
  auto_run: false,
  agent_id: "main",
  reasoning: "medium",
});

export function KanbanBoard({
  workspace,
  onUsePrompt,
}: {
  workspace: string;
  onUsePrompt: (prompt: string) => void;
}) {
  const [tasks, setTasks] = useState<KanbanTask[]>([]);
  const [draft, setDraft] = useState<Partial<KanbanTask> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    if (!workspace) return setTasks([]);
    setError("");
    const query = new URLSearchParams({ workspace });
    const response = await fetch(`/api/kernel/kanban/tasks?${query}`);
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      setError(body?.detail || "Chargement du Kanban impossible.");
      return;
    }
    setTasks(await response.json());
  }

  useEffect(() => {
    if (!workspace) return;
    const controller = new AbortController();
    const query = new URLSearchParams({ workspace });
    void fetch(`/api/kernel/kanban/tasks?${query}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Chargement du Kanban impossible.");
        setTasks(await response.json());
      })
      .catch((caught) => {
        if (caught instanceof Error && caught.name !== "AbortError") setError(caught.message);
      });
    return () => controller.abort();
  }, [workspace]);

  async function save() {
    if (!draft?.title?.trim() || !workspace) return;
    setBusy(true);
    setError("");
    const editing = Boolean(draft.id);
    const response = await fetch(
      editing ? `/api/kernel/kanban/tasks/${draft.id}` : "/api/kernel/kanban/tasks",
      {
        method: editing ? "PATCH" : "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title: draft.title,
          ...(editing ? {} : { workspace }),
          prompt: draft.prompt || "",
          acceptance_criteria: draft.acceptance_criteria || "",
          status: draft.status || "backlog",
          priority: draft.priority || "medium",
          auto_run: Boolean(draft.auto_run),
          agent_id: draft.agent_id || "main",
          reasoning: draft.reasoning || "medium",
        }),
      },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      setError(body?.detail || "Enregistrement impossible.");
    } else {
      setDraft(null);
      await load();
    }
    setBusy(false);
  }

  async function move(task: KanbanTask, status: Status) {
    if (task.status === status || (status === "ready" && !task.prompt.trim())) return;
    setTasks((current) => current.map((item) => item.id === task.id ? { ...item, status } : item));
    const response = await fetch(`/api/kernel/kanban/tasks/${task.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status }),
    });
    if (!response.ok) await load();
  }

  async function remove(task: KanbanTask) {
    if (!window.confirm(`Supprimer « ${task.title} » ?`)) return;
    await fetch(`/api/kernel/kanban/tasks/${task.id}`, { method: "DELETE" });
    await load();
  }

  if (!workspace) {
    return <div className="kanban-empty">Sélectionne un projet pour ouvrir son Kanban.</div>;
  }

  return (
    <div className="kanban-shell">
      <div className="kanban-toolbar">
        <p>Les cartes automatiques en colonne Prêt peuvent être réclamées par un agent cron.</p>
        <button className="primary" onClick={() => setDraft(emptyDraft(workspace))}>
          Nouvelle tâche
        </button>
      </div>
      {error && <p className="management-error" role="alert">{error}</p>}
      <div className="kanban-board">
        {columns.map((column) => (
          <section
            className="kanban-column"
            key={column.id}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              const task = tasks.find((item) => item.id === event.dataTransfer.getData("text/task-id"));
              if (task) void move(task, column.id);
            }}
          >
            <header><strong>{column.label}</strong><span>{tasks.filter((task) => task.status === column.id).length}</span></header>
            <div className="kanban-cards">
              {tasks.filter((task) => task.status === column.id).map((task) => (
                <article
                  className={`kanban-card priority-${task.priority}`}
                  key={task.id}
                  draggable
                  onDragStart={(event) => event.dataTransfer.setData("text/task-id", task.id)}
                >
                  <div className="kanban-card-meta">
                    <span>{task.priority}</span>
                    {task.auto_run && <span className="kanban-auto">auto</span>}
                  </div>
                  <h4>{task.title}</h4>
                  {task.prompt && <p>{task.prompt}</p>}
                  {task.last_error && <small>{task.last_error}</small>}
                  <footer>
                    <button onClick={() => setDraft(task)}>Éditer</button>
                    <button onClick={() => onUsePrompt(task.prompt)} disabled={!task.prompt}>Utiliser</button>
                    <button onClick={() => void remove(task)}>Supprimer</button>
                  </footer>
                </article>
              ))}
            </div>
          </section>
        ))}
      </div>

      {draft && (
        <div className="kanban-editor-backdrop" onMouseDown={() => setDraft(null)}>
          <form className="kanban-editor" onMouseDown={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void save(); }}>
            <h3>{draft.id ? "Modifier la tâche" : "Nouvelle tâche"}</h3>
            <label>Titre<input value={draft.title || ""} onChange={(event) => setDraft({ ...draft, title: event.target.value })} autoFocus /></label>
            <label>Prompt<textarea rows={8} value={draft.prompt || ""} onChange={(event) => setDraft({ ...draft, prompt: event.target.value })} /></label>
            <label>Critères d’acceptation<textarea rows={4} value={draft.acceptance_criteria || ""} onChange={(event) => setDraft({ ...draft, acceptance_criteria: event.target.value })} /></label>
            <div className="kanban-editor-grid">
              <label>Colonne<select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value as Status })}>{columns.map((column) => <option key={column.id} value={column.id}>{column.label}</option>)}</select></label>
              <label>Priorité<select value={draft.priority} onChange={(event) => setDraft({ ...draft, priority: event.target.value as Priority })}>{["low", "medium", "high", "urgent"].map((priority) => <option key={priority}>{priority}</option>)}</select></label>
              <label>Agent<input value={draft.agent_id || "main"} onChange={(event) => setDraft({ ...draft, agent_id: event.target.value })} /></label>
              <label>Thinking<select value={draft.reasoning || "medium"} onChange={(event) => setDraft({ ...draft, reasoning: event.target.value })}>{["minimal", "low", "medium", "high", "xhigh"].map((level) => <option key={level}>{level}</option>)}</select></label>
            </div>
            <label className="kanban-checkbox"><input type="checkbox" checked={Boolean(draft.auto_run)} onChange={(event) => setDraft({ ...draft, auto_run: event.target.checked })} /> Autoriser le dispatcher cron à exécuter cette tâche</label>
            <div className="resource-editor-actions"><button type="button" onClick={() => setDraft(null)}>Annuler</button><button className="primary" disabled={busy || !draft.title?.trim()}>{busy ? "Enregistrement…" : "Enregistrer"}</button></div>
          </form>
        </div>
      )}
    </div>
  );
}
