"use client";

export type Approval = {
  approval_id: string;
  session_id: string;
  tool_name: string;
  tool_description: string;
  action_family: string;
  path: string | null;
  reason: string;
  justification: string;
  arguments: Record<string, unknown>;
  risks: string[];
  run_id?: string;
  created_at?: string;
};

function visibleArguments(approval: Approval) {
  return Object.fromEntries(
    Object.entries(approval.arguments || {}).filter(([key]) => key !== "justification"),
  );
}

function approvalScope(approval: Approval) {
  const target = approval.path ? `la cible « ${approval.path} »` : "toute cible équivalente";
  return `Portée : futurs appels à « ${approval.tool_name} » de type « ${approval.action_family} » pour ${target}, jusqu’à /clear.`;
}

function ApprovalDisclosure({ approval }: { approval: Approval }) {
  const args = visibleArguments(approval);
  return (
    <div className="approval-disclosure">
      <strong>{approval.justification}</strong>
      <p>Outil : <code>{approval.tool_name}</code></p>
      {approval.tool_description && <p>Fonction : {approval.tool_description}</p>}
      {approval.path && <p>Cible : <code>{approval.path}</code></p>}
      {Object.keys(args).length > 0 && (
        <details>
          <summary>Voir les paramètres exacts</summary>
          <pre>{JSON.stringify(args, null, 2)}</pre>
        </details>
      )}
      <p>Pourquoi une confirmation : {approval.reason}</p>
      <small>{approvalScope(approval)}</small>
    </div>
  );
}

type ApprovalPanelProps = {
  approvals: Approval[];
  running: boolean;
  progress: string;
  onResolve: (approval: Approval, approved: boolean) => void;
  onResolveBatch: (approved: boolean) => void;
};

export function ApprovalPanel({
  approvals,
  running,
  progress,
  onResolve,
  onResolveBatch,
}: ApprovalPanelProps) {
  return (
    <>
      {approvals.length > 0 && (
        <section className="approval-stack" aria-live="polite">
          {approvals.length > 1 ? (
            <article className="approval-card approval-batch">
              <div>
                <p className="eyebrow">Autorisation groupée · {approvals.length} actions</p>
                <strong>{approvals.length} actions proposées</strong>
                <ul>
                  {approvals.map((approval) => (
                    <li key={approval.approval_id}>
                      <ApprovalDisclosure approval={approval} />
                    </li>
                  ))}
                </ul>
                <small>Une seule décision sera appliquée à tout ce batch.</small>
              </div>
              <div className="approval-actions">
                <button disabled={running} onClick={() => onResolveBatch(false)}>
                  Tout refuser
                </button>
                <button
                  className="approve"
                  disabled={running}
                  onClick={() => onResolveBatch(true)}
                >
                  Tout autoriser
                </button>
              </div>
            </article>
          ) : approvals.map((approval) => (
            <article className="approval-card" key={approval.approval_id}>
              <div>
                <p className="eyebrow">Autorisation requise · {approval.risks.join(", ")}</p>
                <ApprovalDisclosure approval={approval} />
              </div>
              <div className="approval-actions">
                <button disabled={running} onClick={() => onResolve(approval, false)}>
                  Refuser
                </button>
                <button
                  className="approve"
                  disabled={running}
                  onClick={() => onResolve(approval, true)}
                >
                  Autoriser
                </button>
              </div>
            </article>
          ))}
        </section>
      )}
      {progress && (
        <div className="approval-progress" role="status" aria-live="polite">
          <span className="approval-spinner" />
          <strong>{progress}</strong>
        </div>
      )}
    </>
  );
}
