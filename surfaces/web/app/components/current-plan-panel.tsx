"use client";

import { ChevronDown, GitBranch, Trash2 } from "lucide-react";

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
          ><Trash2 size={19} /></button>
          <button
            type="button"
            onClick={() => onExpandedChange(!expanded)}
            aria-label={expanded ? "Replier le plan" : "Ouvrir le plan"}
          ><ChevronDown size={22} /></button>
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
                <span className="parallel-badge">
                  <GitBranch size={14} /> parallélisable
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
