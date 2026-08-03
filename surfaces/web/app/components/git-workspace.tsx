"use client";

import { GitBranch, X } from "lucide-react";

export type GitFile = {
  path: string;
  status: string;
  staged: boolean;
  additions: number;
  deletions: number;
  binary: boolean;
  patch: string;
  fingerprint?: string;
};

export type GitSnapshot = {
  available: boolean;
  workspace: string;
  repo_root: string | null;
  branch: string | null;
  head: string | null;
  files: GitFile[];
  additions: number;
  deletions: number;
  fingerprint: string;
};

export function GitChangeCard({
  snapshot,
  onOpen,
}: {
  snapshot: GitSnapshot;
  onOpen: (file?: GitFile) => void;
}) {
  if (!snapshot.files.length) return null;
  return (
    <section className="git-change-card">
      <header>
        <div><strong>{snapshot.files.length} fichier{snapshot.files.length > 1 ? "s" : ""} modifié{snapshot.files.length > 1 ? "s" : ""}</strong>
          <span><b>+{snapshot.additions}</b> <i>-{snapshot.deletions}</i></span>
        </div>
        <button type="button" onClick={() => onOpen()}>Examiner</button>
      </header>
      <div>
        {snapshot.files.map((file) => (
          <button type="button" key={file.path} onClick={() => onOpen(file)}>
            <span>{file.path}</span><em><b>+{file.additions}</b> <i>-{file.deletions}</i></em>
          </button>
        ))}
      </div>
    </section>
  );
}

export function GitToolbar({
  snapshot, branches, disabled, onSwitch, onValidate,
}: {
  snapshot: GitSnapshot;
  branches: string[];
  disabled: boolean;
  onSwitch: (branch: string) => void;
  onValidate: () => void;
}) {
  return (
    <div className="git-toolbar">
      <GitBranch aria-hidden="true" />
      <select aria-label="Branche Git" value={snapshot.branch || ""}
        disabled={disabled} onChange={(event) => onSwitch(event.target.value)}>
        {branches.map((branch) => <option key={branch}>{branch}</option>)}
      </select>
      <button type="button" disabled={disabled || !snapshot.files.length} onClick={onValidate}>
        Valider{snapshot.files.length ? ` (${snapshot.files.length})` : ""}
      </button>
    </div>
  );
}

export function GitReviewPanel({
  snapshot, selected, onSelect, onClose, onResizeStart,
}: {
  snapshot: GitSnapshot;
  selected: GitFile | null;
  onSelect: (file: GitFile) => void;
  onClose: () => void;
  onResizeStart: () => void;
}) {
  const file = selected || snapshot.files[0] || null;
  return (
    <aside className="git-review-panel">
      <button type="button" className="git-resizer" aria-label="Redimensionner le panneau"
        onMouseDown={onResizeStart} />
      <header><div><small>Révision · {snapshot.branch}</small><strong>{snapshot.files.length} fichiers · <b>+{snapshot.additions}</b> <i>-{snapshot.deletions}</i></strong></div>
        <button type="button" onClick={onClose} aria-label="Fermer"><X /></button></header>
      <div className="git-review-body">
        <nav>{snapshot.files.map((item) => <button type="button" key={item.path}
          className={file?.path === item.path ? "active" : ""} onClick={() => onSelect(item)}>
          <span>{item.path}</span><em><b>+{item.additions}</b> <i>-{item.deletions}</i></em></button>)}</nav>
        <section><h3>{file?.path || "Aucun fichier"}</h3>
          {file?.binary ? <p>Fichier binaire — aperçu indisponible.</p> : <pre>{file?.patch || "Aucune différence textuelle."}</pre>}
        </section>
      </div>
    </aside>
  );
}

export function GitCommitDialog({
  message, busy, error, source, onMessage, onCancel, onCommit,
}: {
  message: string;
  busy: boolean;
  error: string;
  source: string;
  onMessage: (message: string) => void;
  onCancel: () => void;
  onCommit: () => void;
}) {
  return <div className="git-dialog-backdrop"><section className="git-dialog" role="dialog" aria-modal="true" aria-labelledby="git-dialog-title">
    <header><div><small>Tout le dépôt courant</small><h2 id="git-dialog-title">Valider les modifications</h2></div><button type="button" onClick={onCancel} disabled={busy}><X /></button></header>
    <p>Ce commit inclura toutes les modifications du workspace, pas seulement celles de la dernière réponse.</p>
    <label>Message de commit<textarea autoFocus value={message} onChange={(event) => onMessage(event.target.value)} rows={7} /></label>
    {source === "fallback" && <small>Le modèle était indisponible : un message local a été proposé.</small>}
    {error && <div className="git-dialog-error" role="alert">{error}</div>}
    <footer><button type="button" onClick={onCancel} disabled={busy}>Annuler</button><button type="button" className="primary" onClick={onCommit} disabled={busy || !message.trim()}>{busy ? "Validation…" : "Créer le commit"}</button></footer>
  </section></div>;
}
