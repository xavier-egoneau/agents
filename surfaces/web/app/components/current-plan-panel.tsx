"use client";

import { Icon } from "../theme/theme-context";

export type PlanStep = {
  id: string;
  title: string;
  status: "pending" | "claimed" | "in_progress" | "validating" | "completed" | "blocked" | "failed";
  dependencies: string[];
  parallelizable: boolean;
  write_scopes?: string[];
  claimed_by?: string | null;
  lease_until?: string | null;
  run_id?: string | null;
  note?: string | null;
};

export type CurrentPlan = {
  plan_id: string;
  title: string;
  steps: PlanStep[];
};

/**
 * Qui a réellement pris la tâche, ou rien si elle n'a été déléguée à personne.
 *
 * Le kernel préfixe `claimed_by` : `agent:<nom>` pour un agent configuré,
 * `subagent:<rôle>` pour un exécutant créé à la volée. Une tâche exécutée par
 * l'orchestrateur lui-même n'a pas de préfixe — et ce cas-là, précisément,
 * signifie que la parallélisation déclarée n'a pas eu lieu.
 */
function delegatedTo(step: PlanStep): string {
  const [prefixe, ...reste] = (step.claimed_by || "").split(":");
  return prefixe === "agent" || prefixe === "subagent" ? reste.join(":") : "";
}

type CurrentPlanPanelProps = {
  plan: CurrentPlan;
  expanded: boolean;
  running: boolean;
  onExpandedChange: (expanded: boolean) => void;
  onDelete: () => void;
  onStepChange: (step: PlanStep, completed: boolean) => void;
};

export function CurrentPlanPanel({
  plan,
  expanded,
  running,
  onExpandedChange,
  onDelete,
  onStepChange,
}: CurrentPlanPanelProps) {
  const completed = plan.steps.filter((step) => step.status === "completed").length;
  return (
    <section className={`current-plan ${expanded ? "expanded" : ""}`}>
      <header>
        <div>
          <p className="eyebrow">Plan courant</p>
          <strong>{completed} tâches sur {plan.steps.length} terminées</strong>
          <small>{plan.title}</small>
        </div>
        <div className="plan-actions">
          <button
            type="button"
            onClick={onDelete}
            aria-label="Supprimer le plan"
            title="Supprimer le plan"
          ><Icon name="remove" size="md" /></button>
          <button
            type="button"
            onClick={() => onExpandedChange(!expanded)}
            aria-label={expanded ? "Replier le plan" : "Ouvrir le plan"}
          ><Icon name="chevronDown" size="lg" /></button>
        </div>
      </header>
      {expanded && (
        <div className="plan-steps">
          {plan.steps.map((step) => (
            <label className={`plan-step ${step.status}`} key={step.id}>
              <input
                type="checkbox"
                checked={step.status === "completed"}
                disabled={running}
                onChange={(event) => onStepChange(step, event.target.checked)}
              />
              <span className="plan-step-copy">
                <strong>{step.id} · {step.title}</strong>
                <small>
                  {step.dependencies?.length
                    ? `Dépend de ${step.dependencies.join(", ")}`
                    : "Sans dépendance"}
                  {step.note ? ` · ${step.note}` : ""}
                </small>
              </span>
              {step.parallelizable && (
                // `parallélisable` n'est qu'une déclaration faite au moment du
                // plan; `claimed_by` dit qui l'a réellement prise. Confondre les
                // deux laissait croire à un parallélisme qui n'avait pas eu lieu.
                <span
                  className="parallel-badge"
                  data-delegated={delegatedTo(step) ? "true" : undefined}
                  title={
                    delegatedTo(step)
                      ? `Exécutée par ${delegatedTo(step)}`
                      : "Déclarée parallélisable au moment du plan"
                  }
                >
                  <Icon name="git" size="xs" />
                  {delegatedTo(step) || "parallélisable"}
                </span>
              )}
              {step.status === "in_progress" && <span className="step-running" />}
            </label>
          ))}
        </div>
      )}
    </section>
  );
}
