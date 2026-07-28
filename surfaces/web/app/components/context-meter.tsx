"use client";

export type ContextStatus = {
  provider_id: string;
  model: string | null;
  context_window_tokens: number | null;
  estimated_history_tokens: number;
  estimated_ratio: number | null;
  compaction_threshold_ratio: number;
  compaction_count: number;
};

function compactTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value);
}

export function ContextMeter({ status }: { status: ContextStatus | null }) {
  const ratio = status?.estimated_ratio;
  const windowTokens = status?.context_window_tokens;
  return (
    <div
      className={[
        "context-meter",
        ratio == null ? "unknown" : "",
        (ratio || 0) >= 0.7 ? "critical" : (ratio || 0) >= 0.5 ? "warning" : "",
      ].filter(Boolean).join(" ")}
      title={
        windowTokens
          ? `${status.estimated_history_tokens.toLocaleString("fr-FR")} tokens estimés sur ${windowTokens.toLocaleString("fr-FR")}`
          : "Fenêtre de contexte inconnue — utilise /model-context"
      }
    >
      <div className="context-meter-label">
        <span>Contexte</span>
        {ratio != null && windowTokens ? (
          <strong>
            {Math.min(100, Math.round(ratio * 100))} %
            <small>
              {compactTokens(status.estimated_history_tokens)} / {compactTokens(windowTokens)}
            </small>
          </strong>
        ) : (
          <strong>Fenêtre inconnue <small>/model-context</small></strong>
        )}
      </div>
      <div className="context-meter-track" aria-hidden="true">
        <span
          className="context-meter-fill"
          style={{ width: ratio == null ? "100%" : `${Math.min(100, ratio * 100)}%` }}
        />
        <i className="context-threshold" title="Compaction automatique à 70 %" />
      </div>
    </div>
  );
}
