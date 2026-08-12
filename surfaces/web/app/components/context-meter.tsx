"use client";

export type ContextStatus = {
  provider_id: string;
  model: string | null;
  context_window_tokens: number | null;
  estimated_history_tokens: number;
  estimated_request_tokens?: number;
  observed_input_tokens?: number | null;
  last_run_total_input_tokens?: number | null;
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
  const threshold = status?.compaction_threshold_ratio ?? 0.7;
  const windowTokens = status?.context_window_tokens;
  const displayedTokens = status?.estimated_request_tokens
    ?? status?.estimated_history_tokens
    ?? 0;
  // Trois états, et non deux. Une conversation vide n'a pas de ratio mesuré,
  // mais sa fenêtre est connue : afficher « fenêtre inconnue » y était faux, et
  // envoyait chercher du côté de `/model-context` un réglage déjà fait.
  const enAttente = status === null;
  const ratio = windowTokens
    ? status?.estimated_ratio ?? displayedTokens / windowTokens
    : null;
  return (
    <div
      className={[
        "context-meter",
        ratio == null ? "unknown" : "",
        (ratio || 0) >= threshold ? "critical" : (ratio || 0) >= threshold - 0.2 ? "warning" : "",
      ].filter(Boolean).join(" ")}
      title={
        windowTokens
          ? `${displayedTokens.toLocaleString("fr-FR")} tokens sur ${windowTokens.toLocaleString("fr-FR")}`
          : enAttente
            ? "Mesure du contexte en cours"
            : "Fenêtre de contexte inconnue — utilise /model-context"
      }
    >
      <div className="context-meter-label">
        <span>Contexte</span>
        {ratio != null && windowTokens ? (
          <strong>
            {Math.min(100, Math.round(ratio * 100))} %
            <small>
              {compactTokens(displayedTokens)} / {compactTokens(windowTokens)}
            </small>
          </strong>
        ) : enAttente ? (
          <strong><small>mesure en cours…</small></strong>
        ) : (
          <strong>Fenêtre inconnue <small>/model-context</small></strong>
        )}
      </div>
      <div className="context-meter-track" aria-hidden="true">
        <span
          className="context-meter-fill"
          style={{ width: ratio == null ? "100%" : `${Math.min(100, ratio * 100)}%` }}
        />
        <i
          className="context-threshold"
          style={{ left: `${threshold * 100}%` }}
          title={`Compaction automatique à ${Math.round(threshold * 100)} %`}
        />
      </div>
    </div>
  );
}
