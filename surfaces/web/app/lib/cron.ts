import { type Approval } from "../components/approval-panel";
import {
  type RoutineWorkflow,
  type RoutineWorkflowProposal,
} from "../components/routine-workflow-panel";
import { isRecord } from "./api";
import { countLabel } from "./format";

export type CronJob = {
  id: string;
  name: string;
  schedule: string;
  prompt: string;
  workspace: string | null;
  agent_id: string;
  skills: string[];
  security_mode: "safe" | "limited" | "power";
  provider_id: string | null;
  model: string | null;
  reasoning: "minimal" | "low" | "medium" | "high" | "xhigh" | null;
  enabled: boolean;
  auto_resume: boolean;
  session_id: string;
  notification_session_id: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  last_retryable: boolean;
  in_flight: boolean;
  blocked: boolean;
  workflow?: RoutineWorkflow | null;
  workflow_revision?: number | null;
  workflow_basis_hash?: string | null;
  workflow_updated_at?: string | null;
  one_shot_at?: string | null;
};

export type CronRun = {
  id: string;
  cron_job_id: string;
  scheduled_for: string;
  claimed_at: string;
  started_at: string | null;
  completed_at: string | null;
  session_id: string;
  notification_session_id: string;
  run_id: string | null;
  execution_status: string;
  task_status: string | null;
  delivery_status: "unread" | "read";
  output_preview: string | null;
  error: string | null;
};

export type CronApprovalStatus = {
  approved_scopes: { tool_name: string; action_family: string; path: string | null }[];
  pending_count: number;
  pending_run_id: string | null;
};

export type CronTestFeedback = "idle" | "progress" | "success" | "error";
export type CronWorkflowAction = "propose" | "continue" | "accept" | "delete" | null;

export type CronTestResult = {
  session_id: string;
  run_id?: string | null;
  status: string;
  output?: string | null;
  errors?: { message: string }[];
};

export type CronFrequencyKind = "minutes" | "hours" | "daily" | "weekly" | "yearly" | "once";
export type CronEditor = CronJob & {
  creating: boolean;
  frequency_kind: CronFrequencyKind;
  frequency_interval: number;
  frequency_time: string;
  frequency_weekday: number;
  frequency_month: number;
  frequency_monthday: number;
  one_shot_datetime: string;
};

export function parseCronFrequency(schedule: string, one_shot_at?: string | null): Pick<CronEditor,
  "frequency_kind" | "frequency_interval" | "frequency_time" |
  "frequency_weekday" | "frequency_month" | "frequency_monthday" | "one_shot_datetime"> {
  const parts = schedule.trim().split(/\s+/);
  const defaults = {
    frequency_kind: "daily" as CronFrequencyKind,
    frequency_interval: 1,
    frequency_time: "09:00",
    frequency_weekday: 1,
    frequency_month: 1,
    frequency_monthday: 1,
    one_shot_datetime: "",
  };
  if (!schedule && one_shot_at) {
    const d = new Date(one_shot_at);
    const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
    return { ...defaults, frequency_kind: "once", one_shot_datetime: local };
  }
  if (parts.length !== 5) return defaults;
  const [minute, hour, monthday, month, weekday] = parts;
  if (minute.startsWith("*/") && hour === "*" && monthday === "*" && month === "*" && weekday === "*") {
    return { ...defaults, frequency_kind: "minutes", frequency_interval: Number(minute.slice(2)) || 1 };
  }
  if (minute === "0" && hour.startsWith("*/") && monthday === "*" && month === "*" && weekday === "*") {
    return { ...defaults, frequency_kind: "hours", frequency_interval: Number(hour.slice(2)) || 1 };
  }
  const time = `${String(Number(hour)).padStart(2, "0")}:${String(Number(minute)).padStart(2, "0")}`;
  if (monthday === "*" && month === "*" && weekday === "*") {
    return { ...defaults, frequency_kind: "daily", frequency_time: time };
  }
  if (monthday === "*" && month === "*" && weekday !== "*") {
    return { ...defaults, frequency_kind: "weekly", frequency_time: time, frequency_weekday: Number(weekday) };
  }
  if (monthday !== "*" && month !== "*" && weekday === "*") {
    return {
      ...defaults, frequency_kind: "yearly", frequency_time: time,
      frequency_monthday: Number(monthday), frequency_month: Number(month),
    };
  }
  return defaults;
}

export function cronEditorFromJob(job: CronJob, creating = false): CronEditor {
  return { ...job, creating, ...parseCronFrequency(job.schedule, job.one_shot_at) };
}

export function groupRoutineApprovals(approvals: Approval[]) {
  const grouped = new Map<string, {
    key: string;
    tool_name: string;
    path: string | null;
    count: number;
    justifications: string[];
  }>();
  for (const approval of approvals) {
    const key = [approval.tool_name, approval.action_family, approval.path || ""].join("::");
    const existing = grouped.get(key);
    if (existing) {
      existing.count += 1;
      if (!existing.justifications.includes(approval.justification)) {
        existing.justifications.push(approval.justification);
      }
    } else {
      grouped.set(key, {
        key,
        tool_name: approval.tool_name,
        path: approval.path,
        count: 1,
        justifications: [approval.justification],
      });
    }
  }
  return [...grouped.values()];
}

export function scheduleFromEditor(editor: CronEditor): string {
  const [hour = "9", minute = "0"] = editor.frequency_time.split(":");
  if (editor.frequency_kind === "minutes") return `*/${Math.max(1, editor.frequency_interval)} * * * *`;
  if (editor.frequency_kind === "hours") return `0 */${Math.max(1, editor.frequency_interval)} * * *`;
  if (editor.frequency_kind === "once") return "";
  if (editor.frequency_kind === "weekly") return `${Number(minute)} ${Number(hour)} * * ${editor.frequency_weekday}`;
  if (editor.frequency_kind === "yearly") {
    return `${Number(minute)} ${Number(hour)} ${editor.frequency_monthday} ${editor.frequency_month} *`;
  }
  return `${Number(minute)} ${Number(hour)} * * *`;
}

export function describeCron(editor: CronEditor): string {
  const days = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"];
  const months = ["janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre"];
  if (editor.frequency_kind === "minutes") {
    return `Toutes les ${countLabel(editor.frequency_interval, "minute")}`;
  }
  if (editor.frequency_kind === "hours") {
    return `Toutes les ${countLabel(editor.frequency_interval, "heure")}`;
  }
  if (editor.frequency_kind === "once") {
    const d = editor.one_shot_datetime
      ? new Date(editor.one_shot_datetime)
      : null;
    if (d && !isNaN(d.getTime())) {
      return `Le ${d.toLocaleDateString("fr-FR")} à ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
    }
    return "Date ponctuelle";
  }
  if (editor.frequency_kind === "weekly") return `Chaque ${days[editor.frequency_weekday]} à ${editor.frequency_time}`;
  if (editor.frequency_kind === "yearly") {
    return `Tous les ans, le ${editor.frequency_monthday} ${months[editor.frequency_month - 1]} à ${editor.frequency_time}`;
  }
  return `Tous les jours à ${editor.frequency_time}`;
}

export function cronRequestBody(editor: CronEditor, acceptedWorkflow?: RoutineWorkflowProposal) {
  return {
    name: editor.name,
    schedule: scheduleFromEditor(editor),
    prompt: editor.prompt,
    workspace: editor.workspace,
    agent_id: editor.agent_id,
    skills: editor.skills,
    security_mode: editor.security_mode,
    provider_id: editor.provider_id,
    model: editor.model,
    reasoning: editor.reasoning,
    enabled: editor.enabled,
    auto_resume: editor.auto_resume,
    notification_session_id: editor.notification_session_id,
    ...(editor.frequency_kind === "once" && editor.one_shot_datetime
      ? { one_shot_at: new Date(editor.one_shot_datetime).toISOString() }
      : {}),
    ...(acceptedWorkflow
      ? {
        accepted_workflow: {
          workflow: acceptedWorkflow.workflow,
          basis_hash: acceptedWorkflow.basis_hash,
          // Le kernel attend `list[str]`. La forme `{message}` n'existe que
          // pour l'affichage : la renvoyer telle quelle faisait refuser le
          // corps par Pydantic, une erreur par avertissement.
          warnings: acceptedWorkflow.warnings.map(
            (warning) => typeof warning === "string" ? warning : warning.message || "",
          ).filter(Boolean),
        },
      }
      : {}),
  };
}

export class WorkflowProposalDisplayError extends Error {}

/**
 * Récupère le `detail` renvoyé par le kernel. Sans lui, un échec de génération
 * (502) se réduit à « vérifie le fournisseur » alors que le backend explique
 * précisément ce qui a échoué — le diagnostic était jeté avec le corps.
 */
export async function workflowProposalError(response: Response): Promise<WorkflowProposalDisplayError> {
  let detail = "";
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && typeof payload.detail === "string") detail = payload.detail.trim();
  } catch {
    // The status-specific message remains useful when a proxy returns no JSON.
  }
  return workflowProposalErrorForStatus(response.status, detail);
}

function workflowProposalErrorForStatus(
  status: number,
  detail = "",
): WorkflowProposalDisplayError {
  if (status === 404) {
    return new WorkflowProposalDisplayError(
      "La proposition guidée n’est pas encore chargée dans l’application active. "
      + "Redémarre l’application puis réessaie. La routine reste utilisable en mode libre.",
    );
  }
  if (status === 401 || status === 403) {
    return new WorkflowProposalDisplayError(
      "La connexion au fournisseur du modèle n’est plus valide. "
      + "Vérifie sa configuration, puis réessaie.",
    );
  }
  if (status === 408 || status === 429 || status === 503 || status === 504) {
    return new WorkflowProposalDisplayError(
      "Le fournisseur du modèle est temporairement indisponible. "
      + "Attends un instant, puis réessaie.",
    );
  }
  if (status === 502) {
    return new WorkflowProposalDisplayError(
      "Le modèle a répondu, mais sa réponse n’a pas pu être transformée en workflow.",
    );
  }
  if (status === 422) {
    return new WorkflowProposalDisplayError(
      detail || (
        "Le prompt, les skills ou les outils disponibles ne permettent pas encore de préparer un workflow. "
        + "Ajuste la routine, puis réessaie."
      ),
    );
  }
  return new WorkflowProposalDisplayError(
    "Le modèle n’a pas pu préparer le workflow. "
    + "Vérifie le fournisseur et le modèle configurés, puis réessaie.",
  );
}

export function workflowProposalFromPayload(payload: unknown): RoutineWorkflowProposal {
  if (!isRecord(payload) || !isRecord(payload.workflow) || typeof payload.basis_hash !== "string") {
    throw new Error("La proposition de workflow est incomplète.");
  }
  const warnings = Array.isArray(payload.warnings)
    ? payload.warnings.filter((item) => typeof item === "string" || isRecord(item))
      .map((item) => typeof item === "string" ? item : { message: String(item.message || "Point à vérifier") })
    : [];
  return {
    workflow: payload.workflow as RoutineWorkflow,
    basis_hash: payload.basis_hash,
    warnings,
  };
}

export function workflowStateFromPayload(payload: unknown): {
  workflow: RoutineWorkflow | null;
  revision?: number | null;
  basisHash?: string | null;
  updatedAt?: string | null;
} {
  if (payload === null) return { workflow: null };
  if (!isRecord(payload)) return { workflow: null };
  if ("workflow" in payload) {
    return {
      workflow: isRecord(payload.workflow) ? payload.workflow as RoutineWorkflow : null,
      revision: typeof payload.workflow_revision === "number"
        ? payload.workflow_revision
        : typeof payload.revision === "number" ? payload.revision : null,
      basisHash: typeof payload.workflow_basis_hash === "string"
        ? payload.workflow_basis_hash
        : typeof payload.basis_hash === "string" ? payload.basis_hash : null,
      updatedAt: typeof payload.workflow_updated_at === "string"
        ? payload.workflow_updated_at
        : typeof payload.updated_at === "string" ? payload.updated_at : null,
    };
  }
  const looksLikeWorkflow = Array.isArray(payload.steps)
    || typeof payload.schema === "string"
    || typeof payload.status === "string";
  return { workflow: looksLikeWorkflow ? payload as RoutineWorkflow : null };
}
