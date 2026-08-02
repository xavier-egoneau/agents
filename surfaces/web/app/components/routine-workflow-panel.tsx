export type RoutineWorkflowStep = {
  id?: string;
  title?: string;
  label?: string;
  description?: string;
  instructions?: string;
  kind?: string;
  tool?: string | null;
  needs?: string[];
  args?: Record<string, unknown>;
};

export type RoutineWorkflowDependency = {
  capability?: string;
  reason?: string;
};

export type RoutineWorkflow = Record<string, unknown> & {
  id?: string;
  title?: string;
  status?: string;
  execution?: {
    mode?: string;
    deviation?: string;
  };
  steps?: RoutineWorkflowStep[];
  missing_dependencies?: Array<RoutineWorkflowDependency | string>;
};

export type RoutineWorkflowProposal = {
  workflow: RoutineWorkflow;
  basis_hash: string;
  warnings: Array<string | { message?: string }>;
};

type RoutineWorkflowPanelProps = {
  workflow: RoutineWorkflow | null;
  proposal: RoutineWorkflowProposal | null;
  revision?: number | null;
  updatedAt?: string | null;
  action: "propose" | "continue" | "accept" | "delete" | null;
  feedback: "idle" | "success" | "warning" | "error";
  message: string;
  creating: boolean;
  canPropose: boolean;
  canContinueWithoutWorkflow: boolean;
  canAcceptProposal: boolean;
  canDelete: boolean;
  onPropose: () => void;
  onContinueWithoutWorkflow: () => void;
  onAcceptProposal: () => void;
  onDelete: () => void;
};

function dependencyLabel(dependency: RoutineWorkflowDependency | string) {
  if (typeof dependency === "string") return dependency;
  return [dependency.capability, dependency.reason].filter(Boolean).join(" · ");
}

function warningLabel(warning: string | { message?: string }) {
  return typeof warning === "string" ? warning : warning.message || "Point à vérifier";
}

function workflowFreedom(workflow: RoutineWorkflow) {
  const declared = workflow.remaining_freedom || workflow.freedom;
  if (typeof declared === "string" && declared.trim()) return declared;
  if (workflow.execution?.deviation === "stop_and_report") {
    return "Faible · l’agent s’arrête et signale tout écart non prévu.";
  }
  if (workflow.execution?.mode === "agent_guided") {
    return "Guidée · les étapes sont fixées, la synthèse reste confiée à l’agent.";
  }
  return "Guidée par les étapes et les outils ci-dessous.";
}

const toolLabels: Record<string, string> = {
  web_search: "Recherche web",
  get_agenda: "Agenda",
  get_calendar: "Calendrier",
  list_calendar_events: "Événements du calendrier",
  read_file: "Lecture de fichier",
  write_file: "Écriture de fichier",
};

const argumentLabels: Record<string, string> = {
  query: "Recherche",
  date: "Date",
  start: "Début",
  end: "Fin",
  limit: "Nombre maximum",
  path: "Chemin",
  timezone: "Fuseau horaire",
  url: "Adresse",
};

function toolLabel(tool: string) {
  return toolLabels[tool] || "Outil de la routine";
}

function stepTitle(step: RoutineWorkflowStep, index: number) {
  if (step.title || step.label) return step.title || step.label || `Étape ${index + 1}`;
  if (step.kind === "synthesize") return "Synthétiser les résultats";
  if (step.tool === "web_search") return "Rechercher les informations utiles";
  if (["get_agenda", "get_calendar", "list_calendar_events"].includes(step.tool || "")) {
    return "Consulter l’agenda";
  }
  if (step.tool) return "Exécuter l’outil prévu";
  return `Étape ${index + 1}`;
}

function workflowStatusLabel(status: string) {
  return ({ ready: "Prêt", blocked: "Bloqué", draft: "Brouillon" } as Record<string, string>)[
    status.toLowerCase()
  ] || status;
}

function stepDescription(step: RoutineWorkflowStep) {
  if (step.description?.trim()) return step.description;
  if (step.instructions?.trim()) return step.instructions;
  const justification = step.args?.justification;
  if (typeof justification === "string" && justification.trim()) return justification;
  if (step.kind === "synthesize") {
    return "Rassembler les éléments obtenus et produire le résultat demandé par la routine.";
  }
  return "Exécuter cette étape avec les paramètres prévus ci-dessous.";
}

function argumentLabel(name: string) {
  if (argumentLabels[name]) return argumentLabels[name];
  const label = name.replace(/[-_]+/g, " ");
  return `${label.slice(0, 1).toUpperCase()}${label.slice(1)}`;
}

function argumentValue(value: unknown) {
  if (value === null) return "Aucune valeur";
  if (typeof value === "boolean") return value ? "Oui" : "Non";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return JSON.stringify(value, null, 2);
}

function stepArguments(step: RoutineWorkflowStep) {
  return Object.entries(step.args || {}).filter(([name]) => name !== "justification");
}

function WorkflowPreview({ workflow }: { workflow: RoutineWorkflow }) {
  const steps = Array.isArray(workflow.steps) ? workflow.steps : [];
  const dependencies = Array.isArray(workflow.missing_dependencies)
    ? workflow.missing_dependencies
    : [];
  const titlesById = new Map(
    steps.flatMap((step, index) => step.id ? [[step.id, stepTitle(step, index)]] : []),
  );

  return (
    <div className="routine-workflow-preview">
      <div className="routine-workflow-meta">
        <span>Liberté restante</span>
        <strong>{workflowFreedom(workflow)}</strong>
      </div>
      {steps.length > 0 ? (
        <div className="routine-workflow-sequence">
          <div className="routine-workflow-sequence-heading">
            <strong>Déroulé proposé</strong>
            <span>{steps.length} étape{steps.length > 1 ? "s" : ""}</span>
          </div>
          <ol className="routine-workflow-steps">
            {steps.map((step, index) => {
              const args = stepArguments(step);
              const friendlyToolLabel = step.tool ? toolLabel(step.tool) : "";
              return (
                <li key={step.id || `${step.tool || "step"}-${index}`}>
                  <span className="routine-workflow-step-number" aria-hidden="true">{index + 1}</span>
                  <article className="routine-workflow-step-content">
                    <header>
                      <strong>{stepTitle(step, index)}</strong>
                      <span>{step.kind === "synthesize" ? "Synthèse" : "Action"}</span>
                    </header>
                    <p>{stepDescription(step)}</p>
                    {step.tool && (
                      <div className="routine-workflow-step-tool">
                        <span>Outil</span>
                        <div>
                          <strong>{friendlyToolLabel}</strong>
                          <code>{step.tool}</code>
                        </div>
                      </div>
                    )}
                    {step.needs && step.needs.length > 0 && (
                      <p className="routine-workflow-step-needs">
                        Après : {step.needs.map((need) => titlesById.get(need) || need).join(", ")}
                      </p>
                    )}
                    {args.length > 0 && (
                      <dl className="routine-workflow-step-args">
                        {args.map(([name, value]) => (
                          <div key={name}>
                            <dt>{argumentLabel(name)}</dt>
                            <dd><code>{argumentValue(value)}</code></dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </article>
                </li>
              );
            })}
          </ol>
        </div>
      ) : (
        <p className="routine-workflow-empty">Aucune étape exploitable dans cette proposition.</p>
      )}
      {dependencies.length > 0 && (
        <div className="routine-workflow-dependencies">
          <strong>Dépendances requises</strong>
          <ul>
            {dependencies.map((dependency, index) => (
              <li key={`${dependencyLabel(dependency)}-${index}`}>
                {dependencyLabel(dependency)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function RoutineWorkflowPanel({
  workflow,
  proposal,
  revision,
  updatedAt,
  action,
  feedback,
  message,
  creating,
  canPropose,
  canContinueWithoutWorkflow,
  canAcceptProposal,
  canDelete,
  onPropose,
  onContinueWithoutWorkflow,
  onAcceptProposal,
  onDelete,
}: RoutineWorkflowPanelProps) {
  const displayed = proposal?.workflow || workflow;
  const busy = action !== null;
  const warnings = proposal?.warnings || [];
  const saved = Boolean(workflow && !proposal);

  return (
    <section
      className={`routine-workflow-panel ${proposal ? "proposal" : saved ? "saved" : "empty"} ${feedback}`}
      aria-busy={busy}
      aria-labelledby="routine-workflow-title"
    >
      <div className="routine-workflow-heading">
        <div>
          <span className="routine-workflow-kicker">
            {proposal
              ? "Choix à confirmer"
              : saved
                ? "Mode d’exécution actuel"
                : "Deux chemins possibles"}
          </span>
          <strong id="routine-workflow-title">
            {proposal
              ? "Proposition non enregistrée"
              : saved
                ? `${workflow?.status === "ready" ? "Workflow actif" : "Workflow enregistré"}${revision ? ` · v${revision}` : ""}`
                : "Mode libre — sans workflow"}
          </strong>
          <small>
            {proposal
              ? "Vérifie le déroulé proposé, puis choisis de garder le mode actuel ou d’appliquer ce guide."
              : saved
                ? workflow?.status === "ready"
                  ? "La routine suit ce guide en plus de son prompt et de ses skills."
                  : "Ce workflow est conservé, mais la routine ne peut pas l’exécuter tant qu’il n’est pas prêt."
                : "Le workflow est facultatif : la routine peut fonctionner librement, ou suivre un déroulé que tu valides."}
          </small>
        </div>
        {displayed?.status && (
          <span className={`routine-workflow-status ${String(displayed.status).toLowerCase()}`}>
            {workflowStatusLabel(String(displayed.status))}
          </span>
        )}
      </div>

      {!displayed && (
        <div className="routine-workflow-paths" aria-label="Choix du mode d’exécution">
          <article className="routine-workflow-path current">
            <span>Choix 1 · actuel</span>
            <strong>Continuer en mode libre</strong>
            <p>
              L’agent suit le prompt et les skills, puis choisit ses outils et leur ordre à chaque
              lancement.
            </p>
            <button
              type="button"
              className="secondary"
              disabled={busy || !canContinueWithoutWorkflow}
              onClick={onContinueWithoutWorkflow}
            >
              {action === "continue"
                ? creating ? "Création en cours…" : "Enregistrement…"
                : creating ? "Créer en mode libre" : "Continuer en mode libre"}
            </button>
          </article>
          <article className="routine-workflow-path guided">
            <span>Choix 2 · facultatif</span>
            <strong>Optimiser avec un workflow proposé</strong>
            <p>
              L’outil prépare des étapes précises à relire. Rien n’est appliqué sans ta validation.
            </p>
            <button type="button" disabled={busy || !canPropose} onClick={onPropose}>
              {action === "propose" ? "Proposition en cours…" : "Préparer un workflow guidé"}
            </button>
          </article>
        </div>
      )}

      {displayed && <WorkflowPreview workflow={displayed} />}

      {warnings.length > 0 && (
        <div className="routine-workflow-warnings">
          <strong>Points à vérifier</strong>
          <ul>
            {warnings.map((warning, index) => (
              <li key={`${warningLabel(warning)}-${index}`}>{warningLabel(warning)}</li>
            ))}
          </ul>
        </div>
      )}

      {saved && updatedAt && (
        <small className="routine-workflow-date">
          Mis à jour {new Date(updatedAt).toLocaleString("fr-FR")}
        </small>
      )}

      {message && (
        <p
          className="routine-workflow-feedback"
          role={feedback === "error" ? "alert" : "status"}
          aria-live="polite"
        >
          {busy && <span className="cron-test-spinner" aria-hidden="true" />}
          {message}
        </p>
      )}

      {proposal && (
        <div className="routine-workflow-decision">
          <div>
            <span>Décision</span>
            <strong>Garder la routine actuelle ou appliquer cette proposition ?</strong>
            <small>
              Le prompt et les skills restent actifs dans les deux cas.
            </small>
          </div>
          <div className="routine-workflow-actions">
            <button
              type="button"
              disabled={busy || !canContinueWithoutWorkflow}
              onClick={onContinueWithoutWorkflow}
            >
              {action === "continue"
                ? "Enregistrement du mode actuel…"
                : workflow ? "Conserver le workflow actuel" : "Continuer sans workflow"}
            </button>
            <button
              type="button"
              className="primary"
              disabled={busy || !canAcceptProposal}
              onClick={onAcceptProposal}
            >
              {action === "accept" ? "Acceptation en cours…" : "Accepter ce workflow"}
            </button>
          </div>
        </div>
      )}

      {!creating && saved && (
        <div className="routine-workflow-actions">
          <button type="button" disabled={busy || !canPropose} onClick={onPropose}>
            {action === "propose" ? "Proposition en cours…" : "Proposer une version optimisée"}
          </button>
          <button type="button" className="danger" disabled={busy || !canDelete} onClick={onDelete}>
            {action === "delete" ? "Suppression…" : "Supprimer le workflow…"}
          </button>
        </div>
      )}
    </section>
  );
}
