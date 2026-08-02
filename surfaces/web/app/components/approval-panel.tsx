"use client";

export type Approval = {
  approval_id: string;
  session_id: string;
  tool_name: string;
  action_family: string;
  path: string | null;
  reason: string;
  justification: string;
  risks: string[];
  run_id?: string;
  created_at?: string;
};

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
                    <li key={approval.approval_id}>{approval.justification}</li>
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
                <strong>{approval.tool_name}</strong>
                {approval.path && <code>{approval.path}</code>}
                <p>{approval.justification}</p>
                <small>{approval.reason}</small>
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
