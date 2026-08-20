export type TraceEvent = {
  sequence?: number;
  timestamp: string;
  session_id: string;
  run_id: string;
  agent_id: string;
  parent_run_id: string | null;
  type: string;
  attempt: number;
  payload: Record<string, unknown>;
};

export const visibleTraceTypes = new Set([
  "session.started", "run.suspended", "run.resumed", "run.transitioned",
  "agent.queued", "agent.started", "agent.retrying", "agent.completed", "agent.failed",
  "tool.proposed", "guardian.reviewed", "approval.requested", "approval.resolved",
  "tool.started", "tool.completed", "tool.failed", "tool.trashed", "session.completed",
  "tool.cache_hit",
  "security.changed", "context.pre_compaction_snapshot", "context.compacted",
  "context.compaction_noop",
  "context.inspected", "context.window_updated", "context.window_update_failed",
]);

export function traceLabel(event: TraceEvent) {
  const tool = String(event.payload.tool || event.payload.tool_name || "outil");
  const labels: Record<string, string> = {
    "session.started": "Analyse de la demande",
    "agent.started": `Délégation à ${event.agent_id}`,
    "agent.retrying": `Nouvelle tentative de ${event.agent_id}`,
    "agent.completed": `${event.agent_id} a terminé`,
    "agent.failed": `${event.agent_id} a échoué`,
    "tool.proposed": `Préparation de ${tool}`,
    "guardian.reviewed": `Guardian · ${String(event.payload.verdict || "revue")}`,
    "approval.requested": `Autorisation requise pour ${tool}`,
    "approval.resolved": event.payload.approved ? "Action autorisée" : "Action refusée",
    "tool.started": `${tool} en cours`,
    "tool.completed": `${tool} terminé`,
    "tool.cache_hit": `${tool} déjà disponible`,
    "tool.failed": `${tool} a échoué`,
    "tool.trashed": "Élément déplacé dans la corbeille",
    "session.completed": "Réponse terminée",
    "security.changed": `Permissions · ${String(event.payload.security_mode || "")}`,
    "context.pre_compaction_snapshot": "Préservation du contexte complet",
    "context.compacted": event.payload.manual
      ? "Compaction manuelle terminée"
      : "Compaction automatique terminée",
    "context.compaction_noop": event.payload.manual
      ? "Compaction manuelle sans effet"
      : "Compaction automatique sans effet",
    "context.inspected": "Mesure du contexte",
    "context.window_updated": "Fenêtre de contexte enregistrée",
    "context.window_update_failed": "Fenêtre de contexte invalide",
  };
  return labels[event.type] || event.type;
}

/**
 * Ce qui mérite d'être signalé sur une étape sans la dérouler.
 *
 * Le plafonnement des sorties d'outil se décidait entièrement dans
 * `metadata.truncated`, invisible depuis l'interface : impossible de savoir si
 * un résultat avait été abrégé, ni si l'agent repartait chercher l'intégralité
 * — les deux façons dont le plafonnement peut mal tourner.
 */
export function traceFlag(event: TraceEvent): string | null {
  const chemin = String(event.payload.path || "");
  if (chemin.includes("artifacts") && event.type.startsWith("tool.")) {
    return "relit un résultat mis de côté";
  }
  if (event.type !== "tool.completed") return null;
  const result = event.payload.result as Record<string, unknown> | undefined;
  const metadata = result?.metadata as Record<string, unknown> | undefined;
  const abrege = metadata?.truncated as Record<string, unknown> | undefined;
  return abrege ? "résultat abrégé" : null;
}

/**
 * Le texte qui explique une étape, quelle que soit la forme du champ.
 *
 * `String(payload.error)` affichait `[object Object]` : les erreurs d'outils
 * sont des objets `{type, message}`. L'étape disait donc qu'un `plan_claim`
 * avait échoué sans jamais dire pourquoi — précisément l'information dont on a
 * besoin pour comprendre, et la seule que le déroulé pouvait fournir.
 */
export function traceDetail(event: TraceEvent): string {
  const lisible = (valeur: unknown): string => {
    if (typeof valeur === "string") return valeur;
    if (valeur && typeof valeur === "object") {
      const objet = valeur as Record<string, unknown>;
      const message = typeof objet.message === "string" ? objet.message : "";
      const type = typeof objet.type === "string" ? objet.type : "";
      if (message) return type ? `${type} — ${message}` : message;
      return JSON.stringify(valeur);
    }
    return "";
  };
  for (const cle of ["justification", "task", "reason", "message", "error"]) {
    const texte = lisible(event.payload[cle]);
    if (texte) return texte;
  }
  const arguments_ = event.payload.arguments;
  if (arguments_ && typeof arguments_ === "object") {
    const values = arguments_ as Record<string, unknown>;
    const justification = lisible(values.justification);
    if (justification) return justification;
    for (const cle of ["path", "query", "program", "url"]) {
      const value = lisible(values[cle]);
      if (value) return value;
    }
  }
  if (event.type === "context.compacted") {
    const before = Number(event.payload.estimated_tokens_before);
    const after = Number(event.payload.estimated_tokens_after);
    if (Number.isFinite(before) && Number.isFinite(after)) {
      return `Contexte réduit de ${before.toLocaleString("fr-FR")} à ${after.toLocaleString("fr-FR")} tokens estimés.`;
    }
  }
  if (event.type === "context.compaction_noop") {
    const before = Number(event.payload.estimated_tokens_before);
    if (Number.isFinite(before)) {
      return `Aucun contenu réductible (${before.toLocaleString("fr-FR")} tokens estimés).`;
    }
  }
  return "";
}

export function traceState(event: TraceEvent, isLast: boolean, live: boolean) {
  if (event.type.includes("failed")) return "failed";
  if (event.type === "approval.requested") return "blocked";
  if (event.type === "session.completed") {
    return event.payload.status === "failed" || event.payload.status === "timeout" ? "failed" : "done";
  }
  if (live && isLast) return "active";
  return "done";
}

export type RunStats = {
  /** Tokens envoyés au modèle : instructions, historique, schémas d'outils. */
  inputTokens: number;
  /** Tokens réellement écrits par le modèle. */
  outputTokens: number;
  /** Mur à mur, de `session.started` à `session.completed`. */
  durationMs: number;
  /** Temps passé dans les outils, déductible de la durée. */
  toolMs: number;
  /** Temps passé à attendre une autorisation humaine. */
  waitMs: number;
  /**
   * Vitesse d'écriture réelle, chronométrée par le serveur d'inférence.
   * Absente pour les providers distants, qui ne publient pas la décomposition
   * entre lecture du prompt et écriture de la réponse.
   */
  generationPerSecond?: number;
  /** Vitesse de lecture du prompt, même origine. */
  prefillPerSecond?: number;
};

/**
 * Décompte de chaque run.
 *
 * On a longtemps affiché `outputTokens / durationMs` sous le nom « tokens/s ».
 * C'était faux et trompeur : cette division met au dénominateur le traitement
 * du prompt, les appels d'outils et l'attente d'une autorisation humaine. Une
 * réponse de 82 tokens précédée d'un prompt de 26 000 affichait « 3 tokens/s »
 * alors que le modèle écrivait à 100 — au point de faire diagnostiquer une
 * panne matérielle qui n'existait pas.
 *
 * On expose donc les termes séparément et on laisse l'affichage dire ce qu'il
 * sait vraiment, plutôt qu'un ratio qui a l'air d'une vitesse sans en être une.
 */
export function runStatsByRunId(events: TraceEvent[]): Map<string, RunStats> {
  const startedAt = new Map<string, number>();
  const toolMs = new Map<string, number>();
  const waitMs = new Map<string, number>();
  const openTool = new Map<string, number>();
  const openWait = new Map<string, number>();
  const stats = new Map<string, RunStats>();
  const accumulate = (
    totals: Map<string, number>,
    open: Map<string, number>,
    runId: string,
    at: number,
  ) => {
    const since = open.get(runId);
    if (since === undefined) return;
    open.delete(runId);
    totals.set(runId, (totals.get(runId) ?? 0) + (at - since));
  };
  for (const event of events) {
    const at = Date.parse(event.timestamp);
    switch (event.type) {
      case "session.started":
        startedAt.set(event.run_id, at);
        continue;
      case "tool.started":
        openTool.set(event.run_id, at);
        continue;
      case "tool.completed":
      case "tool.failed":
        accumulate(toolMs, openTool, event.run_id, at);
        continue;
      case "approval.requested":
        openWait.set(event.run_id, at);
        continue;
      case "approval.resolved":
        accumulate(waitMs, openWait, event.run_id, at);
        continue;
      case "session.completed":
        break;
      default:
        continue;
    }
    const usage = event.payload.usage;
    const read = (key: string) =>
      usage && typeof usage === "object"
        ? Number((usage as Record<string, unknown>)[key])
        : NaN;
    const outputTokens = read("output_tokens");
    const inputTokens = read("input_tokens");
    const details = usage && typeof usage === "object"
      ? (usage as Record<string, unknown>).details
      : undefined;
    const debit = (key: string) => {
      const valeur = details && typeof details === "object"
        ? Number((details as Record<string, unknown>)[key])
        : NaN;
      return Number.isFinite(valeur) && valeur > 0 ? valeur : undefined;
    };
    const started = startedAt.get(event.run_id);
    const durationMs = started !== undefined ? at - started : NaN;
    if (Number.isFinite(outputTokens) && Number.isFinite(durationMs)) {
      stats.set(event.run_id, {
        inputTokens: Number.isFinite(inputTokens) ? inputTokens : 0,
        outputTokens,
        durationMs,
        toolMs: toolMs.get(event.run_id) ?? 0,
        waitMs: waitMs.get(event.run_id) ?? 0,
        generationPerSecond: debit("generation_tokens_per_second"),
        prefillPerSecond: debit("prefill_tokens_per_second"),
      });
    }
  }
  return stats;
}

export function traceEventsForRun(events: TraceEvent[], rootRunId?: string) {
  if (!rootRunId) return [];
  const included = new Set([rootRunId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const event of events) {
      if (event.parent_run_id && included.has(event.parent_run_id) && !included.has(event.run_id)) {
        included.add(event.run_id);
        changed = true;
      }
    }
  }
  return events.filter((event) => included.has(event.run_id));
}
