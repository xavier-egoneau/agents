"use client";

import {
  FormEvent,
  Fragment,
  SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import {
  ApprovalPanel,
  type Approval,
} from "./components/approval-panel";
import {
  ComposerControls,
  type ReasoningLevel,
  type SecurityMode,
} from "./components/composer-controls";
import {
  ContextMeter,
  type ContextStatus,
} from "./components/context-meter";
import { SessionHistory } from "./components/session-history";
import {
  ResourceNavigation,
  type ResourceSection,
} from "./components/resource-navigation";
import {
  CurrentPlanPanel,
  type CurrentPlan,
  type PlanStep,
} from "./components/current-plan-panel";
import {
  RoutineWorkflowPanel,
  type RoutineWorkflow,
  type RoutineWorkflowProposal,
} from "./components/routine-workflow-panel";
import {
  CalendarClock,
  MessageSquarePlus,
} from "lucide-react";

type Agent = {
  id: string;
  description: string;
  provider: string;
  model: string | null;
  skills: string[];
  delegates: string[];
};

type Skill = { name: string; description: string };
type SlashCommand = {
  command: string;
  description: string;
  kind: string;
  skill: string;
  source: string;
};
type Catalog = {
  default_provider: string;
  agents: Agent[];
  skills: Skill[];
  providers: {
    id: string;
    connection_type: string;
    model: string | null;
    models: string[];
    vision: boolean;
  }[];
  tools: { name: string; description: string; module: string; risks: string[] }[];
};

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  meta?: string;
  error?: boolean;
  runId?: string;
  artifacts?: RunArtifact[];
};

type RunArtifact = {
  artifact_id: string;
  name: string;
  media_type: string;
  kind: string;
  bytes?: number;
};

type Workspace = {
  path: string;
  name: string;
  readable: boolean;
  writable: boolean;
};

type ComposerImage = {
  id: string;
  name: string;
  mediaType: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  dataUrl: string;
};

type ManagedResource = {
  id: string;
  description: string;
  content: string;
};

type ResourceEditor = {
  kind: "agents" | "skills";
  id: string;
  frontmatter: Record<string, unknown>;
  body: string;
  creating: boolean;
};

type ManagedProvider = {
  id: string;
  kind: string;
  connection_type?: "local" | "api_key" | "auth";
  base_url?: string | null;
  model?: string | null;
  models?: string[];
  api_key?: string;
  api_key_configured?: boolean;
  vision?: boolean;
  timeout_seconds?: number;
  auth_help_url?: string;
};

type CronJob = {
  id: string;
  name: string;
  schedule: string;
  prompt: string;
  workspace: string;
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
};

type CronRun = {
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

type CronApprovalStatus = {
  approved_scopes: { tool_name: string; action_family: string; path: string | null }[];
  pending_count: number;
  pending_run_id: string | null;
};

type CronTestFeedback = "idle" | "progress" | "success" | "error";
type CronWorkflowAction = "propose" | "accept" | "delete" | null;

type CronTestResult = {
  session_id: string;
  run_id?: string | null;
  status: string;
  output?: string | null;
  errors?: { message: string }[];
};

type CronFrequencyKind = "minutes" | "hours" | "daily" | "weekly" | "yearly";
type CronEditor = CronJob & {
  creating: boolean;
  frequency_kind: CronFrequencyKind;
  frequency_interval: number;
  frequency_time: string;
  frequency_weekday: number;
  frequency_month: number;
  frequency_monthday: number;
};

function parseCronFrequency(schedule: string): Pick<CronEditor,
  "frequency_kind" | "frequency_interval" | "frequency_time" |
  "frequency_weekday" | "frequency_month" | "frequency_monthday"> {
  const parts = schedule.trim().split(/\s+/);
  const defaults = {
    frequency_kind: "daily" as CronFrequencyKind,
    frequency_interval: 1,
    frequency_time: "09:00",
    frequency_weekday: 1,
    frequency_month: 1,
    frequency_monthday: 1,
  };
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

function cronEditorFromJob(job: CronJob, creating = false): CronEditor {
  return { ...job, creating, ...parseCronFrequency(job.schedule) };
}

function countLabel(count: number, singular: string, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

async function readApiPayload<T = Record<string, unknown>>(response: Response): Promise<T> {
  const text = await response.text();
  let payload: unknown = {};
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      if (!response.ok) {
        throw new Error(`Le serveur n’a pas pu traiter la demande (${response.status}).`);
      }
      throw new Error("Réponse serveur illisible.");
    }
  }
  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : null;
    throw new Error(String(detail || `La demande a échoué (${response.status}).`));
  }
  return payload as T;
}

function groupRoutineApprovals(approvals: Approval[]) {
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

function scheduleFromEditor(editor: CronEditor): string {
  const [hour = "9", minute = "0"] = editor.frequency_time.split(":");
  if (editor.frequency_kind === "minutes") return `*/${Math.max(1, editor.frequency_interval)} * * * *`;
  if (editor.frequency_kind === "hours") return `0 */${Math.max(1, editor.frequency_interval)} * * *`;
  if (editor.frequency_kind === "weekly") return `${Number(minute)} ${Number(hour)} * * ${editor.frequency_weekday}`;
  if (editor.frequency_kind === "yearly") {
    return `${Number(minute)} ${Number(hour)} ${editor.frequency_monthday} ${editor.frequency_month} *`;
  }
  return `${Number(minute)} ${Number(hour)} * * *`;
}

function describeCron(editor: CronEditor): string {
  const days = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"];
  const months = ["janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre"];
  if (editor.frequency_kind === "minutes") {
    return `Toutes les ${countLabel(editor.frequency_interval, "minute")}`;
  }
  if (editor.frequency_kind === "hours") {
    return `Toutes les ${countLabel(editor.frequency_interval, "heure")}`;
  }
  if (editor.frequency_kind === "weekly") return `Chaque ${days[editor.frequency_weekday]} à ${editor.frequency_time}`;
  if (editor.frequency_kind === "yearly") {
    return `Tous les ans, le ${editor.frequency_monthday} ${months[editor.frequency_month - 1]} à ${editor.frequency_time}`;
  }
  return `Tous les jours à ${editor.frequency_time}`;
}

function cronRequestBody(editor: CronEditor, acceptedWorkflow?: RoutineWorkflowProposal) {
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
    ...(acceptedWorkflow ? { accepted_workflow: acceptedWorkflow } : {}),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

class WorkflowProposalDisplayError extends Error {}

function workflowProposalErrorForStatus(status: number): WorkflowProposalDisplayError {
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
  if (status === 422) {
    return new WorkflowProposalDisplayError(
      "Le prompt, les skills ou les outils disponibles ne permettent pas encore de préparer un workflow. "
      + "Ajuste la routine, puis réessaie.",
    );
  }
  return new WorkflowProposalDisplayError(
    "Le modèle n’a pas pu préparer le workflow. "
    + "Vérifie le fournisseur et le modèle configurés, puis réessaie.",
  );
}

function workflowProposalFromPayload(payload: unknown): RoutineWorkflowProposal {
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

function workflowStateFromPayload(payload: unknown): {
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

const codexAuthHelpUrl = "https://learn.chatgpt.com/docs/auth?surface=cli";
const codexApiUrl = "https://chatgpt.com/backend-api/codex";
const providerKindLabels: Record<string, string> = {
  "openai-codex": "Codex — connexion ChatGPT",
  openai: "OpenAI API — clé API",
  deepseek: "DeepSeek — clé API",
  "llama-cpp": "llama.cpp — local",
  "claude-oauth": "Claude — connexion OAuth",
  anthropic: "Anthropic API — clé API",
};

type ComposerPreferences = {
  securityMode: "safe" | "limited" | "power";
  providerId: string;
  model: string;
  reasoning: "minimal" | "low" | "medium" | "high" | "xhigh";
};

const composerPreferencesKey = "amk.composer.preferences.v1";

function parseMarkdownResource(content: string) {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) return { frontmatter: {}, body: content };
  return {
    frontmatter: (parseYaml(match[1]) || {}) as Record<string, unknown>,
    body: match[2].replace(/\s+$/, ""),
  };
}

function buildMarkdownResource(editor: ResourceEditor) {
  const frontmatter = {
    ...editor.frontmatter,
    [editor.kind === "agents" ? "id" : "name"]: editor.id,
  };
  return `---\n${stringifyYaml(frontmatter).trimEnd()}\n---\n${editor.body.trimEnd()}\n`;
}

function resourceList(value: unknown) {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") return value.split(",").map((item) => item.trim()).filter(Boolean);
  return [];
}

function parseListFieldValue(value: string) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

type TraceEvent = {
  timestamp: string;
  session_id: string;
  run_id: string;
  agent_id: string;
  parent_run_id: string | null;
  type: string;
  attempt: number;
  payload: Record<string, unknown>;
};

type SessionSummary = {
  session_id: string;
  agent_id: string;
  prompt: string;
  workspace: string | null;
  created_at: string;
  updated_at: string;
  status: string;
  output: string | null;
  errors: { message: string }[];
  event_count: number;
  trigger?: "user" | "resume" | "cron" | "cron_resume" | "cron_test" | "routine_inbox";
  cron_job_id?: string | null;
  messages?: {
    role: "user" | "assistant";
    content: string;
    error?: boolean;
    run_id?: string;
    artifacts?: RunArtifact[];
  }[];
  events?: TraceEvent[];
};

const visibleTraceTypes = new Set([
  "session.started", "run.suspended", "run.resumed", "run.transitioned",
  "agent.queued", "agent.started", "agent.retrying", "agent.completed", "agent.failed",
  "tool.proposed", "guardian.reviewed", "approval.requested", "approval.resolved",
  "tool.started", "tool.completed", "tool.failed", "tool.trashed", "session.completed",
  "security.changed", "context.pre_compaction_snapshot", "context.compacted",
  "context.inspected", "context.window_updated", "context.window_update_failed",
]);

function traceLabel(event: TraceEvent) {
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
    "tool.failed": `${tool} a échoué`,
    "tool.trashed": "Élément déplacé dans la corbeille",
    "session.completed": "Réponse terminée",
    "security.changed": `Permissions · ${String(event.payload.security_mode || "")}`,
    "context.pre_compaction_snapshot": "Préservation du contexte complet",
    "context.compacted": event.payload.manual
      ? "Compaction manuelle terminée"
      : "Compaction automatique terminée",
    "context.inspected": "Mesure du contexte",
    "context.window_updated": "Fenêtre de contexte enregistrée",
    "context.window_update_failed": "Fenêtre de contexte invalide",
  };
  return labels[event.type] || event.type;
}

function traceState(event: TraceEvent, isLast: boolean, live: boolean) {
  if (event.type.includes("failed")) return "failed";
  if (event.type === "approval.requested") return "blocked";
  if (event.type === "session.completed") {
    return event.payload.status === "failed" || event.payload.status === "timeout" ? "failed" : "done";
  }
  if (live && isLast) return "active";
  return "done";
}

function traceEventsForRun(events: TraceEvent[], rootRunId?: string) {
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

function ProcessTrace({
  events, live,
}: {
  events: TraceEvent[];
  live: boolean;
}) {
  return <ProcessTraceState key={live ? "live" : "terminal"} events={events} live={live} />;
}

function ProcessTraceState({
  events, live,
}: {
  events: TraceEvent[];
  live: boolean;
}) {
  const [expanded, setExpanded] = useState(live);
  const visible = events.filter((event) => visibleTraceTypes.has(event.type));
  if (visible.length === 0) return null;
  return (
    <section className={`process-trace ${expanded ? "expanded" : "collapsed"}`} aria-label="Traces d’exécution" aria-live="polite">
      <button
        type="button"
        className="trace-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <strong>Processus</strong>
        <span className="trace-summary">
          {live && <i className="trace-live-indicator" />}
          {live ? "en cours" : `${visible.length} étape${visible.length > 1 ? "s" : ""}`}
          <b aria-hidden="true">{expanded ? "−" : "+"}</b>
        </span>
      </button>
      {expanded && <ol>
        {visible.map((event, index) => {
          const state = traceState(event, index === visible.length - 1, live);
          const detail = String(
            event.payload.justification || event.payload.task || event.payload.reason ||
            event.payload.message || event.payload.error || "",
          );
          return (
            <li className={state} key={`${event.timestamp}-${event.type}-${index}`}>
              <span className="trace-dot" />
              <div>
                <strong>{traceLabel(event)}</strong>
                <small>{event.agent_id} · {new Date(event.timestamp).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small>
                {detail && <p>{detail}</p>}
              </div>
            </li>
          );
        })}
      </ol>}
    </section>
  );
}

function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...props }) => (
            <a
              {...props}
              href={href}
              target={href?.startsWith("http") ? "_blank" : undefined}
              rel={href?.startsWith("http") ? "noreferrer noopener" : undefined}
            >
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function MessageArtifacts({
  sessionId, artifacts,
}: {
  sessionId: string | null;
  artifacts?: RunArtifact[];
}) {
  if (!sessionId || !artifacts?.length) return null;
  return (
    <div className="message-artifacts">
      {artifacts.map((artifact) => {
        const source = `/api/kernel/artifacts/${encodeURIComponent(sessionId)}/${encodeURIComponent(artifact.artifact_id)}`;
        return artifact.kind === "image" || artifact.media_type.startsWith("image/") ? (
          <figure key={artifact.artifact_id}>
            <a href={source} target="_blank" rel="noreferrer">
              {/* Runtime artifact URLs are not statically optimizable by Next Image. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={source} alt={artifact.name} loading="lazy" />
            </a>
            <figcaption>{artifact.name}</figcaption>
          </figure>
        ) : (
          <a key={artifact.artifact_id} href={source} download={artifact.name}>
            {artifact.name}
          </a>
        );
      })}
    </div>
  );
}

const starterPrompts = [
  "Cartographie les risques et les inconnues de ce projet.",
  "Propose trois sous-tâches indépendantes et délègue-les.",
  "Résume les décisions récentes avec leurs conséquences.",
];

type ConversationState = {
  messages: Message[];
  approvals: Approval[];
  activeSessionId: string | null;
  traceEvents: TraceEvent[];
  activeRunId: string | null;
};

type ConversationAction = {
  type: "set";
  field: keyof ConversationState;
  value: unknown;
};

function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  const current = state[action.field];
  const next = typeof action.value === "function"
    ? (action.value as (previous: typeof current) => typeof current)(current)
    : action.value;
  return { ...state, [action.field]: next };
}

export default function Home() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [agentId, setAgentId] = useState("main");
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [prompt, setPrompt] = useState("");
  const [conversation, dispatchConversation] = useReducer(conversationReducer, {
    messages: [],
    approvals: [],
    activeSessionId: null,
    traceEvents: [],
    activeRunId: null,
  });
  const { messages, approvals, activeSessionId, traceEvents, activeRunId } = conversation;
  const setMessages = useCallback((value: SetStateAction<Message[]>) => {
    dispatchConversation({ type: "set", field: "messages", value });
  }, []);
  const setApprovals = useCallback((value: SetStateAction<Approval[]>) => {
    dispatchConversation({ type: "set", field: "approvals", value });
  }, []);
  const setActiveSessionId = useCallback((value: SetStateAction<string | null>) => {
    dispatchConversation({ type: "set", field: "activeSessionId", value });
  }, []);
  const setTraceEvents = useCallback((value: SetStateAction<TraceEvent[]>) => {
    dispatchConversation({ type: "set", field: "traceEvents", value });
  }, []);
  const setActiveRunId = useCallback((value: SetStateAction<string | null>) => {
    dispatchConversation({ type: "set", field: "activeRunId", value });
  }, []);
  const [running, setRunning] = useState(false);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState("");
  const [workspaceInput, setWorkspaceInput] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [pickingWorkspace, setPickingWorkspace] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [unreadSessionIds, setUnreadSessionIds] = useState<Set<string>>(new Set());
  const [approvalProgress, setApprovalProgress] = useState("");
  const [securityMode, setSecurityMode] = useState<SecurityMode>("limited");
  const [providerId, setProviderId] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [reasoning, setReasoning] = useState<ReasoningLevel>("medium");
  const [composerImages, setComposerImages] = useState<ComposerImage[]>([]);
  const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
  const [attachmentError, setAttachmentError] = useState("");
  const [stopRequested, setStopRequested] = useState(false);
  const [managementModal, setManagementModal] = useState<ResourceSection | null>(null);
  const [managedResources, setManagedResources] = useState<ManagedResource[]>([]);
  const [resourceEditor, setResourceEditor] = useState<ResourceEditor | null>(null);
  const [managementError, setManagementError] = useState("");
  const [savingResource, setSavingResource] = useState(false);
  const [providerModels, setProviderModels] = useState<Record<string, string[]>>({});
  const [modelsLoading, setModelsLoading] = useState(false);
  const [managedProviders, setManagedProviders] = useState<ManagedProvider[]>([]);
  const [defaultProvider, setDefaultProvider] = useState("");
  const [providerEditor, setProviderEditor] = useState<(ManagedProvider & { creating: boolean }) | null>(null);
  const [codexConnected, setCodexConnected] = useState(false);
  const [codexAuthLoading, setCodexAuthLoading] = useState(false);
  const [composerPreferencesReady, setComposerPreferencesReady] = useState(false);
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([]);
  const [commandSelection, setCommandSelection] = useState(0);
  const [currentPlan, setCurrentPlan] = useState<CurrentPlan | null>(null);
  const [planExpanded, setPlanExpanded] = useState(true);
  const [cronJobs, setCronJobs] = useState<CronJob[]>([]);
  const [cronRuns, setCronRuns] = useState<CronRun[]>([]);
  const [cronEditor, setCronEditor] = useState<CronEditor | null>(null);
  const [cronTestApprovals, setCronTestApprovals] = useState<Approval[]>([]);
  const [cronTestMessage, setCronTestMessage] = useState("");
  const [cronTestFeedback, setCronTestFeedback] = useState<CronTestFeedback>("idle");
  const [cronTestDecision, setCronTestDecision] = useState<"approve" | "reject" | null>(null);
  const [cronApprovalStatus, setCronApprovalStatus] = useState<CronApprovalStatus | null>(null);
  const [testingCron, setTestingCron] = useState(false);
  const [cronWorkflowProposal, setCronWorkflowProposal] = useState<RoutineWorkflowProposal | null>(null);
  const [cronWorkflowAction, setCronWorkflowAction] = useState<CronWorkflowAction>(null);
  const [cronWorkflowFeedback, setCronWorkflowFeedback] = useState<"idle" | "success" | "error">("idle");
  const [cronWorkflowMessage, setCronWorkflowMessage] = useState("");
  const railContent = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const runningSessionId = useRef<string | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);
  const sessionSnapshots = useRef(new Map<string, Map<string, string>>());
  const discoveredProviders = useRef(new Set<string>());
  const restoredComposerPreferences = useRef<ComposerPreferences | null>(null);
  const composerPreferencesApplied = useRef(false);
  const lastComposerAgent = useRef<string | null>(null);
  const cronWorkflowProposalRequest = useRef(0);
  const cronWorkflowMutationRequest = useRef(0);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(composerPreferencesKey);
      if (raw) {
        const stored = JSON.parse(raw) as Partial<ComposerPreferences>;
        if (["safe", "limited", "power"].includes(String(stored.securityMode))) {
          setSecurityMode(stored.securityMode as ComposerPreferences["securityMode"]);
        }
        if (typeof stored.providerId === "string") setProviderId(stored.providerId);
        if (typeof stored.model === "string") setSelectedModel(stored.model);
        if (["minimal", "low", "medium", "high", "xhigh"].includes(String(stored.reasoning))) {
          setReasoning(stored.reasoning as ComposerPreferences["reasoning"]);
        }
        restoredComposerPreferences.current = stored as ComposerPreferences;
      }
    } catch {
      window.localStorage.removeItem(composerPreferencesKey);
    } finally {
      setComposerPreferencesReady(true);
    }
  }, []);

  useEffect(() => {
    if (!composerPreferencesReady) return;
    const preferences: ComposerPreferences = {
      securityMode, providerId, model: selectedModel, reasoning,
    };
    restoredComposerPreferences.current = preferences;
    window.localStorage.setItem(composerPreferencesKey, JSON.stringify(preferences));
  }, [composerPreferencesReady, securityMode, providerId, selectedModel, reasoning]);

  useEffect(() => {
    fetch("/api/kernel/catalog")
      .then(async (response) => {
        if (!response.ok) throw new Error("Kernel indisponible");
        return response.json();
      })
      .then((data: Catalog) => {
        setCatalog(data);
        if (data.agents[0]) setAgentId(data.agents[0].id);
      })
      .catch(() => setCatalogError("Démarre le kernel avec `amk serve`."));
  }, []);

  useEffect(() => {
    const stored = window.localStorage.getItem("amk.workspaces");
    let recent: Workspace[] = [];
    try {
      recent = stored ? JSON.parse(stored) : [];
    } catch {
      window.localStorage.removeItem("amk.workspaces");
    }
    fetch("/api/kernel/workspaces/current")
      .then(async (response) => {
        if (!response.ok) throw new Error("Workspace du kernel indisponible");
        return response.json();
      })
      .then((current: Workspace) => {
        // The kernel CWD is only the first-run default. Once the user has made
        // a project list, do not silently add it again on every launch.
        const known = recent.length > 0 ? recent : [current];
        const remembered = window.localStorage.getItem("amk.activeWorkspace");
        const active = known.some((item) => item.path === remembered)
          ? remembered || known[0].path
          : known[0].path;
        setWorkspaces(known);
        setActiveWorkspace(active);
        window.localStorage.setItem("amk.workspaces", JSON.stringify(known));
        window.localStorage.setItem("amk.activeWorkspace", active);
      })
      .catch((error) => setWorkspaceError(error instanceof Error ? error.message : "Erreur workspace"));
  }, []);

  const activeAgent = useMemo(
    () => catalog?.agents.find((agent) => agent.id === agentId),
    [catalog, agentId],
  );
  const activeProvider = useMemo(
    () => catalog?.providers.find((provider) => provider.id === providerId),
    [catalog, providerId],
  );
  const activeWorkspaceInfo = useMemo(
    () => workspaces.find((workspace) => workspace.path === activeWorkspace),
    [workspaces, activeWorkspace],
  );
  const commandMatches = useMemo(() => {
    const value = prompt.trimStart();
    if (!value.startsWith("/") || value.includes(" ")) return [];
    const query = value.toLowerCase();
    return slashCommands.filter((item) => item.command.startsWith(query));
  }, [prompt, slashCommands]);

  useEffect(() => {
    if (!activeWorkspace) return;
    fetch(`/api/kernel/commands?workspace=${encodeURIComponent(activeWorkspace)}`)
      .then((response) => response.ok ? response.json() : [])
      .then((items: SlashCommand[]) => setSlashCommands(items))
      .catch(() => setSlashCommands([]));
  }, [activeWorkspace]);

  const refreshPlan = useCallback(async (sessionId = activeSessionId) => {
    if (!sessionId) {
      setCurrentPlan(null);
      return;
    }
    const response = await fetch(
      `/api/kernel/plans/current?session_id=${encodeURIComponent(sessionId)}`,
    );
    if (response.ok) setCurrentPlan(await response.json());
  }, [activeSessionId]);

  useEffect(() => {
    void refreshPlan();
    if (!running || !activeSessionId) return;
    const timer = window.setInterval(() => void refreshPlan(activeSessionId), 750);
    return () => window.clearInterval(timer);
  }, [activeSessionId, running, refreshPlan]);

  useEffect(() => {
    if (running) setPlanExpanded(true);
  }, [running]);

  async function updatePlanStep(step: PlanStep, completed: boolean) {
    if (!currentPlan) return;
    const response = await fetch(
      `/api/kernel/plans/${encodeURIComponent(currentPlan.plan_id)}/steps/${encodeURIComponent(step.id)}`,
      {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ status: completed ? "completed" : "pending" }),
      },
    );
    if (response.ok) {
      const data: { steps: PlanStep[] } = await response.json();
      setCurrentPlan({ ...currentPlan, steps: data.steps });
    }
  }

  async function deleteCurrentPlan() {
    if (!currentPlan) return;
    const response = await fetch(
      `/api/kernel/plans/${encodeURIComponent(currentPlan.plan_id)}`,
      { method: "DELETE" },
    );
    if (response.ok) setCurrentPlan(null);
  }

  function chooseCommand(command: SlashCommand) {
    setPrompt(`${command.command} `);
    setCommandSelection(0);
    requestAnimationFrame(() => textarea.current?.focus());
  }

  useEffect(() => {
    if (!catalog || !activeAgent || !composerPreferencesReady) return;
    const restored = restoredComposerPreferences.current;
    if (!composerPreferencesApplied.current && restored) {
      const provider = catalog.providers.find((item) => item.id === restored.providerId);
      if (provider) {
        setProviderId(provider.id);
        setSelectedModel(restored.model || provider.model || provider.models[0] || "");
        composerPreferencesApplied.current = true;
        lastComposerAgent.current = activeAgent.id;
        return;
      }
    }
    if (
      composerPreferencesApplied.current
      && lastComposerAgent.current === activeAgent.id
    ) return;
    const provider = catalog.providers.find((item) => item.id === activeAgent.provider)
      || catalog.providers[0];
    if (!provider) return;
    setProviderId(provider.id);
    setSelectedModel(
      provider.models.includes(activeAgent.model || "")
        ? activeAgent.model || provider.model || provider.models[0] || ""
        : provider.model || provider.models[0] || "",
    );
    composerPreferencesApplied.current = true;
    lastComposerAgent.current = activeAgent.id;
  }, [catalog, activeAgent, composerPreferencesReady]);

  useEffect(() => {
    if (!catalog) return;
    const pending = catalog.providers.filter(
      (provider) => !discoveredProviders.current.has(provider.id),
    );
    if (!pending.length) return;
    pending.forEach((provider) => discoveredProviders.current.add(provider.id));
    void Promise.all(pending.map(async (provider) => {
      try {
        const response = await fetch(`/api/kernel/providers/${encodeURIComponent(provider.id)}/models`);
        if (!response.ok) return { id: provider.id, models: provider.models };
        const data: { models: string[] } = await response.json();
        return { id: provider.id, models: data.models.length ? data.models : provider.models };
      } catch {
        return { id: provider.id, models: provider.models };
      }
    })).then((results) => {
      const models = new Map(results.map((result) => [result.id, result.models]));
      setCatalog((current) => current && ({
        ...current,
        providers: current.providers.map((provider) => ({
          ...provider,
          models: models.get(provider.id) || provider.models,
        })),
      }));
    });
  }, [catalog]);

  function selectProvider(nextProviderId: string) {
    const provider = catalog?.providers.find((item) => item.id === nextProviderId);
    setProviderId(nextProviderId);
    if (provider) setSelectedModel(provider.model || provider.models[0] || "");
  }

  async function addImageFiles(files: File[]) {
    setAttachmentError("");
    const accepted = files.filter((file) =>
      ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.type),
    );
    if (accepted.length !== files.length) {
      setAttachmentError("Formats acceptés : PNG, JPEG, WebP et GIF.");
    }
    const available = Math.max(0, 4 - composerImages.length);
    const additions = await Promise.all(accepted.slice(0, available).map(async (file) => {
      if (file.size > 8 * 1024 * 1024) {
        setAttachmentError("Chaque image doit faire moins de 8 Mo.");
        return null;
      }
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      return {
        id: crypto.randomUUID(),
        name: file.name || "image-collée.png",
        mediaType: file.type as ComposerImage["mediaType"],
        dataUrl,
      };
    }));
    setComposerImages((current) => [
      ...current,
      ...additions.filter((item): item is ComposerImage => item !== null),
    ]);
  }

  async function stopRun() {
    const sessionId = runningSessionId.current;
    if (!sessionId || stopRequested) return;
    setStopRequested(true);
    try {
      const response = await fetch(`/api/kernel/runs/${sessionId}/cancel`, { method: "POST" });
      if (!response.ok && response.status !== 404) {
        const data = await response.json();
        throw new Error(data.detail || "Impossible d’arrêter le run");
      }
    } catch (error) {
      setStopRequested(false);
      setAttachmentError(error instanceof Error ? error.message : "Impossible d’arrêter le run");
    }
  }

  async function changeSecurityMode(mode: typeof securityMode) {
    setSecurityMode(mode);
    const sessionId = runningSessionId.current;
    if (!sessionId) return;
    const response = await fetch(`/api/kernel/runs/${sessionId}/security`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ security_mode: mode }),
    });
    if (!response.ok && response.status !== 404) {
      setAttachmentError("Le niveau de permission n’a pas pu être appliqué au run actif.");
    }
  }

  const refreshSessions = useCallback(async (workspace = activeWorkspace) => {
    if (!workspace) return;
    const response = await fetch(`/api/kernel/sessions?workspace=${encodeURIComponent(workspace)}`);
    if (!response.ok) return;
    const nextSessions: SessionSummary[] = await response.json();
    const previous = sessionSnapshots.current.get(workspace);
    const nextSnapshot = new Map(
      nextSessions.map((session) => [session.session_id, session.updated_at]),
    );
    sessionSnapshots.current.set(workspace, nextSnapshot);
    if (previous) {
      const changed = nextSessions
        .filter((session) => previous.get(session.session_id) !== session.updated_at)
        .map((session) => session.session_id);
      if (changed.length) {
        setUnreadSessionIds((current) => {
          const updated = new Set(current);
          changed.forEach((id) => {
            if (id !== activeSessionIdRef.current) updated.add(id);
          });
          return updated;
        });
        if (
          activeSessionIdRef.current
          && changed.includes(activeSessionIdRef.current)
        ) {
          void openSession(activeSessionIdRef.current);
        }
      }
    }
    setSessions(nextSessions);
  // openSession is a hoisted event action; adding it would recreate this polling
  // callback on every render without changing the synchronized inputs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeWorkspace]);

  useEffect(() => {
    void refreshSessions(activeWorkspace);
    if (!activeWorkspace) return;
    const timer = window.setInterval(
      () => void refreshSessions(activeWorkspace),
      2500,
    );
    return () => window.clearInterval(timer);
  }, [activeWorkspace, refreshSessions]);

  const refreshContextStatus = useCallback(async () => {
    if (!providerId) {
      setContextStatus(null);
      return;
    }
    const params = new URLSearchParams({ provider_id: providerId });
    if (selectedModel) params.set("model", selectedModel);
    if (activeSessionId) params.set("session_id", activeSessionId);
    const response = await fetch(`/api/kernel/context-status?${params.toString()}`);
    if (response.ok) setContextStatus(await response.json());
  }, [activeSessionId, providerId, selectedModel]);

  useEffect(() => {
    void refreshContextStatus();
    const timer = window.setInterval(() => void refreshContextStatus(), 5000);
    return () => window.clearInterval(timer);
  }, [refreshContextStatus]);

  useEffect(() => {
    // A project switch changes the entire sidebar context. Do not retain a
    // scroll offset from the previous project, otherwise the active-project
    // card can appear clipped or entirely missing.
    railContent.current?.scrollTo({ top: 0 });
  }, [activeWorkspace]);

  useEffect(() => {
    if (!managementModal) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setManagementModal(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [managementModal]);

  const refreshCrons = useCallback(async () => {
    const [jobsResponse, runsResponse] = await Promise.all([
      fetch("/api/kernel/crons"),
      fetch("/api/kernel/crons/runs?limit=100"),
    ]);
    if (!jobsResponse.ok) throw new Error("Impossible de charger les cronjobs");
    setCronJobs(await jobsResponse.json());
    if (runsResponse.ok) {
      const payload: unknown = await runsResponse.json();
      const runs: CronRun[] = Array.isArray(payload) ? payload : [];
      setCronRuns(runs);
      const unreadSessions = runs
        .filter((item) =>
          item.delivery_status === "unread"
          && ["success", "failed", "partial", "timeout", "blocked"].includes(item.execution_status)
        )
        .map((item) => item.notification_session_id);
      if (unreadSessions.length) {
        setUnreadSessionIds((current) => {
          const updated = new Set(current);
          unreadSessions.forEach((id) => {
            if (id !== activeSessionIdRef.current) updated.add(id);
          });
          return updated;
        });
      }
    }
  }, []);

  useEffect(() => {
    setManagementError("");
    void refreshCrons().catch((error) => {
      if (managementModal === "crons") {
        setManagementError(error instanceof Error ? error.message : "Erreur");
      }
    });
    const timer = window.setInterval(() => void refreshCrons().catch(() => undefined), 2000);
    return () => window.clearInterval(timer);
  }, [managementModal, refreshCrons]);

  useEffect(() => {
    if (managementModal !== "providers") return;
    setManagementError("");
    fetch("/api/kernel/admin/providers")
      .then(async (response) => {
        if (!response.ok) throw new Error("Impossible de charger les providers");
        return response.json();
      })
      .then((data: { default_provider: string; providers: ManagedProvider[] }) => {
        setManagedProviders(data.providers);
        setDefaultProvider(data.default_provider);
      })
      .catch((error) => setManagementError(error instanceof Error ? error.message : "Erreur"));
  }, [managementModal]);

  useEffect(() => {
    if (providerEditor?.kind !== "openai-codex") {
      setCodexConnected(false);
      return;
    }
    fetch("/api/kernel/auth/openai-codex/status")
      .then(async (response) => {
        if (!response.ok) throw new Error("Statut OAuth indisponible");
        return response.json();
      })
      .then((data: { connected: boolean }) => setCodexConnected(data.connected))
      .catch(() => setCodexConnected(false));
  }, [providerEditor?.kind]);

  useEffect(() => {
    if (!resourceEditor || resourceEditor.kind !== "agents") return;
    const provider = String(resourceEditor.frontmatter.provider || catalog?.default_provider || "");
    if (!provider || providerModels[provider]) return;
    setModelsLoading(true);
    fetch(`/api/kernel/providers/${encodeURIComponent(provider)}/models`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Découverte des modèles impossible");
        return response.json();
      })
      .then((data: { models: string[] }) => {
        setProviderModels((current) => ({ ...current, [provider]: data.models }));
      })
      .catch(() => {
        const configured = catalog?.providers.find((item) => item.id === provider)?.models || [];
        setProviderModels((current) => ({ ...current, [provider]: configured }));
      })
      .finally(() => setModelsLoading(false));
  }, [resourceEditor, providerModels, catalog]);

  useEffect(() => {
    if (managementModal !== "agents" && managementModal !== "skills") return;
    setManagementError("");
    fetch(`/api/kernel/admin/${managementModal}`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Impossible de charger les fichiers");
        return response.json();
      })
      .then(setManagedResources)
      .catch((error) => setManagementError(error instanceof Error ? error.message : "Erreur"));
  }, [managementModal]);

  async function refreshCatalog() {
    const response = await fetch("/api/kernel/catalog");
    if (response.ok) setCatalog(await response.json());
  }

  function createResource(kind: "agents" | "skills") {
    const id = kind === "agents" ? "nouvel-agent" : "nouvelle-skill";
    const frontmatter = kind === "agents"
      ? {
          id,
          description: "Nouvel agent",
          provider: catalog?.default_provider || "deepseek",
          model: "",
          tools: catalog?.tools.map((tool) => tool.name) || [],
          skills: [],
          delegates: catalog?.agents.map((agent) => agent.id).filter((agentId) => agentId !== id) || [],
        }
      : { name: id, description: "Nouvelle skill", "allowed-tools": [] };
    setResourceEditor({
      kind,
      id,
      frontmatter,
      body: kind === "agents"
        ? "Instructions de l’agent."
        : "# Instructions\n\nDécris ici le comportement de la skill.",
      creating: true,
    });
    setManagementError("");
  }

  async function saveResource() {
    if (!resourceEditor) return;
    setSavingResource(true);
    setManagementError("");
    const endpoint = `/api/kernel/admin/${resourceEditor.kind}${resourceEditor.creating ? "" : `/${resourceEditor.id}`}`;
    try {
      const response = await fetch(endpoint, {
        method: resourceEditor.creating ? "POST" : "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          id: resourceEditor.id,
          content: buildMarkdownResource(resourceEditor),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Enregistrement impossible");
      const refreshed = await fetch(`/api/kernel/admin/${resourceEditor.kind}`);
      if (refreshed.ok) setManagedResources(await refreshed.json());
      await refreshCatalog();
      setResourceEditor(null);
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Enregistrement impossible");
    } finally {
      setSavingResource(false);
    }
  }

  function updateEditorField(name: string, value: unknown) {
    setResourceEditor((current) => current && ({
      ...current,
      frontmatter: { ...current.frontmatter, [name]: value },
    }));
  }

  function toggleEditorListField(name: string, value: string, defaults: string[] = []) {
    const hasExplicitValue = resourceEditor
      ? Object.prototype.hasOwnProperty.call(resourceEditor.frontmatter, name)
      : false;
    const current = hasExplicitValue
      ? resourceList(resourceEditor?.frontmatter[name])
      : defaults;
    updateEditorField(
      name,
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value],
    );
  }

  function editResource(kind: "agents" | "skills", resource: ManagedResource) {
    const parsed = parseMarkdownResource(resource.content);
    setResourceEditor({
      kind,
      id: resource.id,
      frontmatter: parsed.frontmatter,
      body: parsed.body,
      creating: false,
    });
    setManagementError("");
  }

  async function deleteResource(kind: "agents" | "skills", id: string) {
    if (!window.confirm(`Supprimer ${id} ?`)) return;
    setManagementError("");
    const response = await fetch(`/api/kernel/admin/${kind}/${id}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) {
      setManagementError(data.detail || "Suppression impossible");
      return;
    }
    setManagedResources((current) => current.filter((item) => item.id !== id));
    if (kind === "agents" && agentId === id) setAgentId("main");
    if (kind === "skills") {
      setSelectedSkills((current) => current.filter((skill) => skill !== id));
    }
    await refreshCatalog();
  }

  function createProvider() {
    setProviderEditor({
      id: "nouveau-provider",
      kind: "deepseek",
      connection_type: "api_key",
      base_url: "",
      model: "",
      models: [],
      api_key: "",
      vision: false,
      timeout_seconds: 120,
      creating: true,
    });
    setManagementError("");
  }

  function selectProviderKind(kind: string) {
    setProviderEditor((current) => {
      if (!current) return current;
      // `openai + auth` was selectable in the old generic form although the
      // kernel has no such provider contract. Interpret it as Codex so an
      // existing draft immediately gets the correct dedicated form.
      if (
        kind === "openai-codex"
        || (kind === "openai" && current.connection_type === "auth")
      ) {
        return {
          ...current,
          kind: "openai-codex",
          connection_type: "auth",
          base_url: codexApiUrl,
          model: null,
          models: [],
          timeout_seconds: undefined,
          auth_help_url: codexAuthHelpUrl,
        };
      }
      return { ...current, kind };
    });
  }

  async function saveProvider() {
    if (!providerEditor) return;
    setSavingResource(true);
    setManagementError("");
    const { creating, ...config } = providerEditor;
    const endpoint = `/api/kernel/admin/providers${creating ? "" : `/${providerEditor.id}`}`;
    try {
      const response = await fetch(endpoint, {
        method: creating ? "POST" : "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: providerEditor.id, config }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Enregistrement impossible");
      discoveredProviders.current.delete(providerEditor.id);
      const refreshed = await fetch("/api/kernel/admin/providers");
      if (refreshed.ok) {
        const payload = await refreshed.json();
        setManagedProviders(payload.providers);
        setDefaultProvider(payload.default_provider);
      }
      await refreshCatalog();
      setProviderEditor(null);
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Enregistrement impossible");
    } finally {
      setSavingResource(false);
    }
  }

  async function connectCodex() {
    setCodexAuthLoading(true);
    setManagementError("");
    try {
      const response = await fetch("/api/kernel/auth/openai-codex/login", {
        method: "POST",
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Connexion Codex impossible");
      setCodexConnected(true);
      if (providerEditor && !providerEditor.creating) {
        discoveredProviders.current.delete(providerEditor.id);
        await refreshCatalog();
      }
    } catch (error) {
      setManagementError(
        error instanceof Error ? error.message : "Connexion Codex impossible",
      );
    } finally {
      setCodexAuthLoading(false);
    }
  }

  async function disconnectCodex() {
    setCodexAuthLoading(true);
    setManagementError("");
    try {
      const response = await fetch("/api/kernel/auth/openai-codex/logout", {
        method: "POST",
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Déconnexion impossible");
      setCodexConnected(false);
    } catch (error) {
      setManagementError(
        error instanceof Error ? error.message : "Déconnexion impossible",
      );
    } finally {
      setCodexAuthLoading(false);
    }
  }

  async function deleteProvider(id: string) {
    if (!window.confirm(`Supprimer le provider ${id} ?`)) return;
    const response = await fetch(`/api/kernel/admin/providers/${id}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) {
      setManagementError(data.detail || "Suppression impossible");
      return;
    }
    setManagedProviders((current) => current.filter((provider) => provider.id !== id));
    await refreshCatalog();
  }

  async function deleteSession(sessionId: string) {
    if (!window.confirm("Supprimer définitivement cette session et ses traces ?")) return;
    const response = await fetch(`/api/kernel/sessions/${sessionId}`, { method: "DELETE" });
    if (!response.ok) return;
    setSessions((current) => current.filter((session) => session.session_id !== sessionId));
    if (activeSessionId === sessionId) {
      setActiveSessionId(null);
      setMessages([]);
      setTraceEvents([]);
    }
  }

  async function resumeSession(sessionId: string) {
    setManagementError("");
    setRunning(true);
    setActiveSessionId(sessionId);
    runningSessionId.current = sessionId;
    try {
      const response = await fetch(`/api/kernel/runs/${sessionId}/resume`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Reprise impossible");
      await openSession(sessionId);
      await refreshSessions();
    } catch (error) {
      setAttachmentError(error instanceof Error ? error.message : "Reprise impossible");
    } finally {
      setRunning(false);
      runningSessionId.current = null;
    }
  }

  function resetCronWorkflowState(message = "") {
    cronWorkflowProposalRequest.current += 1;
    cronWorkflowMutationRequest.current += 1;
    setCronWorkflowProposal(null);
    setCronWorkflowAction(null);
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage(message);
  }

  function invalidateCronWorkflowProposal() {
    const hadProposal = Boolean(cronWorkflowProposal || cronWorkflowAction === "propose");
    cronWorkflowProposalRequest.current += 1;
    if (!hadProposal) return;
    setCronWorkflowProposal(null);
    setCronWorkflowAction((current) => current === "propose" ? null : current);
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage(
      cronWorkflowAction === "propose"
        ? "La génération a été annulée car la base de la routine a changé. Relance-la quand tes modifications sont prêtes."
        : "La proposition a été retirée car la base de la routine a changé. Génère-la à nouveau.",
    );
  }

  function closeCronEditor() {
    resetCronWorkflowState();
    setCronEditor(null);
  }

  function createCron() {
    setManagementError("");
    setCronEditor(cronEditorFromJob({
      id: "", name: "Nouvelle routine", schedule: "0 9 * * *", prompt: "",
      workspace: activeWorkspace, agent_id: agentId, skills: selectedSkills,
      security_mode: securityMode, provider_id: providerId || null,
      model: selectedModel || null, reasoning, enabled: false, auto_resume: true,
      session_id: "", notification_session_id: sessions.find((item) => item.trigger === "routine_inbox")?.session_id || "",
      next_run_at: null, last_run_at: null, last_status: null,
      last_error: null, in_flight: false, blocked: false,
      last_retryable: false,
    }, true));
    setCronTestApprovals([]);
    setCronTestMessage("");
    setCronTestFeedback("idle");
    setCronTestDecision(null);
    setCronApprovalStatus(null);
    resetCronWorkflowState();
  }

  async function openCronEditor(job: CronJob) {
    setManagementError("");
    setCronEditor(cronEditorFromJob(job));
    setCronTestApprovals([]);
    setCronTestMessage("Vérification des préautorisations…");
    setCronTestFeedback("progress");
    setCronTestDecision(null);
    setCronApprovalStatus(null);
    resetCronWorkflowState();
    try {
      const response = await fetch(`/api/kernel/crons/${job.id}/approval-status`);
      const status = await readApiPayload<CronApprovalStatus>(response);
      setCronApprovalStatus(status);
      const pending = await refreshCronTestApprovals(job.session_id, status.pending_run_id);
      if (pending.length > 0) {
        const scopeCount = groupRoutineApprovals(pending).length;
        setCronTestMessage(
          `${countLabel(scopeCount, "autorisation")} pour ${countLabel(pending.length, "action")} ${
            scopeCount === 1 ? "attend" : "attendent"
          } ta décision.`,
        );
        setCronTestFeedback("idle");
      } else if (status.approved_scopes.length > 0) {
        setCronTestMessage(
          `Prévalidation active · ${countLabel(status.approved_scopes.length, "périmètre")} ${
            status.approved_scopes.length === 1 ? "autorisé" : "autorisés"
          }.`,
        );
        setCronTestFeedback("success");
      } else {
        setCronTestMessage("Lance un test pour détecter les autorisations nécessaires.");
        setCronTestFeedback("idle");
      }
    } catch (error) {
      setCronTestMessage(
        error instanceof Error ? error.message : "Prévalidations impossibles à charger.",
      );
      setCronTestFeedback("error");
    }
  }

  async function loadCronApprovalStatus(jobId: string) {
    const response = await fetch(`/api/kernel/crons/${jobId}/approval-status`);
    const status = await readApiPayload<CronApprovalStatus>(response);
    setCronApprovalStatus(status);
    return status;
  }

  async function proposeCronWorkflow() {
    if (!cronEditor || cronWorkflowAction) return;
    const requestId = cronWorkflowProposalRequest.current + 1;
    cronWorkflowProposalRequest.current = requestId;
    setCronWorkflowAction("propose");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage("Analyse du prompt, des skills et des outils disponibles…");
    try {
      const response = await fetch("/api/kernel/crons/workflow-proposals", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(cronRequestBody(cronEditor)),
      });
      if (!response.ok) throw workflowProposalErrorForStatus(response.status);
      const proposal = workflowProposalFromPayload(await readApiPayload<unknown>(response));
      if (cronWorkflowProposalRequest.current !== requestId) return;
      setCronWorkflowProposal(proposal);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage(
        proposal.warnings.length > 0
          ? `Proposition prête · ${countLabel(proposal.warnings.length, "point")} à vérifier.`
          : "Proposition prête. Elle ne sera enregistrée qu’après ta validation.",
      );
    } catch (error) {
      if (cronWorkflowProposalRequest.current !== requestId) return;
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(
        error instanceof WorkflowProposalDisplayError
          ? error.message
          : "Impossible de préparer le workflow guidé. Vérifie la connexion de l’application, puis réessaie.",
      );
    } finally {
      if (cronWorkflowProposalRequest.current === requestId) {
        setCronWorkflowAction(null);
      }
    }
  }

  async function acceptCronWorkflow() {
    if (!cronEditor || cronEditor.creating || !cronWorkflowProposal || cronWorkflowAction) return;
    const jobId = cronEditor.id;
    const proposal = cronWorkflowProposal;
    const requestId = cronWorkflowMutationRequest.current + 1;
    cronWorkflowMutationRequest.current = requestId;
    setCronWorkflowAction("accept");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage("Enregistrement du workflow…");
    try {
      const response = await fetch(`/api/kernel/crons/${jobId}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(cronRequestBody(cronEditor, proposal)),
      });
      const updated = await readApiPayload<CronJob>(response);
      if (cronWorkflowMutationRequest.current !== requestId) return;
      setCronEditor((current) => current?.id === jobId ? cronEditorFromJob(updated) : current);
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage(
        "Workflow et configuration enregistrés ensemble. Le prompt et les skills sont conservés.",
      );
      setCronApprovalStatus(null);
      setCronTestApprovals([]);
      setCronTestFeedback("idle");
      setCronTestMessage("Le workflow a changé. Relance un test pour prévalider ses actions.");
      await refreshCrons();
    } catch (error) {
      if (cronWorkflowMutationRequest.current !== requestId) return;
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(error instanceof Error ? error.message : "Enregistrement du workflow impossible.");
    } finally {
      if (cronWorkflowMutationRequest.current === requestId) {
        setCronWorkflowAction(null);
      }
    }
  }

  async function deleteCronWorkflow() {
    if (!cronEditor || cronEditor.creating || !cronEditor.workflow || cronWorkflowAction) return;
    if (!window.confirm(
      "Supprimer ce workflow ? La routine conservera son prompt et ses skills, mais l’agent retrouvera davantage de liberté. Les prévalidations devront être vérifiées à nouveau.",
    )) return;
    const jobId = cronEditor.id;
    const requestId = cronWorkflowMutationRequest.current + 1;
    cronWorkflowMutationRequest.current = requestId;
    setCronWorkflowAction("delete");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage("Suppression du workflow…");
    try {
      const response = await fetch(`/api/kernel/crons/${jobId}/workflow`, { method: "DELETE" });
      const payload = await readApiPayload<unknown>(response);
      if (cronWorkflowMutationRequest.current !== requestId) return;
      const state = workflowStateFromPayload(payload);
      setCronEditor((current) => current?.id === jobId ? ({
        ...current,
        workflow: null,
        workflow_revision: state.revision ?? current.workflow_revision,
        workflow_basis_hash: null,
        workflow_updated_at: null,
      }) : current);
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage("Workflow supprimé · la routine continuera avec son prompt et ses skills.");
      setCronApprovalStatus(null);
      setCronTestApprovals([]);
      setCronTestFeedback("idle");
      setCronTestMessage("Workflow supprimé. Relance un test pour prévalider le mode libre.");
      await refreshCrons();
    } catch (error) {
      if (cronWorkflowMutationRequest.current !== requestId) return;
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(error instanceof Error ? error.message : "Suppression du workflow impossible.");
    } finally {
      if (cronWorkflowMutationRequest.current === requestId) {
        setCronWorkflowAction(null);
      }
    }
  }

  function discardCronWorkflowProposal() {
    setCronWorkflowProposal(null);
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage(
      cronEditor?.workflow
        ? "Proposition ignorée. Le workflow enregistré reste inchangé."
        : "Proposition ignorée. La routine reste sans workflow.",
    );
  }

  async function continueWithoutCronWorkflow() {
    if (!cronEditor || cronWorkflowAction || savingResource || testingCron) return;
    if (cronEditor.workflow && persistedCron && cronWorkflowProposal) {
      setCronEditor(cronEditorFromJob(persistedCron));
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage("Le workflow actuel est conservé sans modification.");
      return;
    }
    await saveCron();
  }

  async function acceptCronWorkflowChoice() {
    if (!cronEditor || !cronWorkflowProposal || cronWorkflowAction) return;
    if (cronEditor.creating) {
      await saveCron(cronWorkflowProposal);
      return;
    }
    await acceptCronWorkflow();
  }

  async function saveCron(acceptedWorkflow?: RoutineWorkflowProposal) {
    if (!cronEditor) return;
    setSavingResource(true);
    setManagementError("");
    const body = cronRequestBody(cronEditor, acceptedWorkflow);
    try {
      const response = await fetch(
        `/api/kernel/crons${cronEditor.creating ? "" : `/${cronEditor.id}`}`,
        {
          method: cronEditor.creating ? "POST" : "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const data = await readApiPayload<CronJob>(response);
      await refreshCrons();
      if (cronEditor.creating) {
        setCronEditor(cronEditorFromJob(data as CronJob));
        setCronWorkflowProposal(null);
        if (acceptedWorkflow) {
          setCronWorkflowFeedback("success");
          setCronWorkflowMessage("Routine et workflow enregistrés. Le prompt et les skills sont conservés.");
        } else {
          setCronWorkflowFeedback("success");
          setCronWorkflowMessage("Routine enregistrée sans workflow · exécution par prompt et skills.");
        }
        setCronTestMessage("Routine enregistrée et inactive. Teste-la avant de l’activer.");
      } else {
        closeCronEditor();
      }
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Enregistrement impossible");
    } finally {
      setSavingResource(false);
    }
  }

  async function deleteCron(id: string) {
    if (!window.confirm("Supprimer ce cronjob ? Son historique de session sera conservé.")) return;
    try {
      const response = await fetch(`/api/kernel/crons/${id}`, { method: "DELETE" });
      await readApiPayload(response);
      await refreshCrons();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Suppression impossible");
    }
  }

  async function runCronNow(id: string) {
    try {
      const response = await fetch(`/api/kernel/crons/${id}/run`, { method: "POST" });
      const data = await readApiPayload<{ notification_session_id?: string }>(response);
      await refreshCrons();
      const notificationSessionId = data.notification_session_id;
      if (notificationSessionId) {
        setUnreadSessionIds((current) => new Set(current).add(notificationSessionId));
      }
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Lancement impossible");
    }
  }

  async function toggleCron(job: CronJob, enabled: boolean) {
    const body = {
      name: job.name, schedule: job.schedule, prompt: job.prompt,
      workspace: job.workspace, agent_id: job.agent_id, skills: job.skills,
      security_mode: job.security_mode, provider_id: job.provider_id,
      model: job.model, reasoning: job.reasoning, enabled,
      auto_resume: job.auto_resume,
      notification_session_id: job.notification_session_id,
    };
    try {
      const response = await fetch(`/api/kernel/crons/${job.id}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      await readApiPayload<CronJob>(response);
      await refreshCrons();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Modification impossible");
    }
  }

  async function refreshCronTestApprovals(sessionId: string, runId?: string | null) {
    const response = await fetch("/api/kernel/approvals");
    const payload = await readApiPayload<Approval[]>(response);
    const pending: Approval[] = Array.isArray(payload) ? payload : [];
    const sessionApprovals = pending.filter((item) => item.session_id === sessionId);
    const selectedRunId = runId || sessionApprovals
      .slice()
      .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)))[0]
      ?.run_id;
    const routineApprovals = selectedRunId
      ? sessionApprovals.filter((item) => item.run_id === selectedRunId)
      : [];
    setCronTestApprovals(routineApprovals);
    return routineApprovals;
  }

  async function testCron(id: string) {
    setTestingCron(true);
    setCronTestMessage("Test en cours…");
    setCronTestFeedback("progress");
    setCronTestDecision(null);
    setCronTestApprovals([]);
    try {
      const response = await fetch(`/api/kernel/crons/${id}/test`, { method: "POST" });
      const data = await readApiPayload<CronTestResult>(response);
      if (data.status === "approval_pending") {
        const pending = await refreshCronTestApprovals(data.session_id, data.run_id);
        const scopeCount = groupRoutineApprovals(pending).length;
        setCronTestMessage(
          `${countLabel(scopeCount, "autorisation")} ${scopeCount === 1 ? "couvre" : "couvrent"} ${
            countLabel(pending.length, "action")
          } ${pending.length === 1 ? "prévue" : "prévues"}.`,
        );
        setCronTestFeedback("idle");
      } else if (data.status === "success") {
        setCronTestMessage("Test réussi. La routine est prête.");
        setCronTestFeedback("success");
      } else {
        setCronTestMessage(data.errors?.map((error: {message: string}) => error.message).join("\n")
          || `Test terminé avec le statut ${data.status}.`);
        setCronTestFeedback("error");
      }
      await refreshCrons();
    } catch (error) {
      setCronTestMessage(error instanceof Error ? error.message : "Test impossible");
      setCronTestFeedback("error");
    } finally {
      setTestingCron(false);
    }
  }

  async function resolveCronTestApprovals(approved: boolean) {
    if (!cronEditor || cronTestApprovals.length === 0) return;
    const current = cronTestApprovals;
    const scopeCount = groupRoutineApprovals(current).length;
    setTestingCron(true);
    setCronTestDecision(approved ? "approve" : "reject");
    setCronTestFeedback("progress");
    setCronTestMessage(approved
      ? `Validation de ${countLabel(scopeCount, "autorisation")} couvrant ${
        countLabel(current.length, "action")
      } · reprise du test…`
      : `Enregistrement de ${countLabel(current.length, "refus", "refus")} · reprise du test…`);
    try {
      const response = await fetch("/api/kernel/approvals/resolve-batch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          approval_ids: current.map((approval) => approval.approval_id),
          approved,
        }),
      });
      const data = await readApiPayload<CronTestResult>(response);
      if (data.status === "approval_pending") {
        const pending = await refreshCronTestApprovals(cronEditor.session_id, data.run_id);
        const scopeCount = groupRoutineApprovals(pending).length;
        setCronTestMessage(
          `${scopeCount} ${scopeCount === 1 ? "nouvelle autorisation" : "nouvelles autorisations"} pour ${
            countLabel(pending.length, "action")
          } à examiner.`,
        );
        setCronTestFeedback("idle");
      } else if (data.status === "success") {
        setCronTestApprovals([]);
        setCronTestMessage(
          approved
            ? `Confirmé · ${countLabel(scopeCount, "autorisation")} ${
              scopeCount === 1 ? "enregistrée" : "enregistrées"
            } pour ${countLabel(current.length, "action")}. Test réussi.`
            : `Confirmé · ${countLabel(current.length, "refus", "refus")} ${
              current.length === 1 ? "enregistré" : "enregistrés"
            }. Test terminé.`,
        );
        setCronTestFeedback("success");
      } else {
        setCronTestApprovals([]);
        setCronTestMessage(
          `${approved ? "Autorisations enregistrées" : "Refus enregistrés"}, mais la reprise du test a échoué : ${
            data.errors?.map((error: {message: string}) => error.message).join("\n")
              || data.output || data.status
          }`,
        );
        setCronTestFeedback("error");
      }
      await loadCronApprovalStatus(cronEditor.id);
      await refreshCrons();
    } catch (error) {
      try {
        const pending = await refreshCronTestApprovals(cronEditor.session_id);
        if (pending.length === 0) {
          setCronTestMessage(
            "La décision a été reçue, mais la reprise du test n’a pas pu être confirmée.",
          );
        } else {
          setCronTestMessage(error instanceof Error ? error.message : "Résolution impossible");
        }
      } catch {
        setCronTestApprovals(current);
        setCronTestMessage("Impossible de vérifier si la décision a été enregistrée. Réessaie dans un instant.");
      }
      setCronTestFeedback("error");
    } finally {
      setTestingCron(false);
      setCronTestDecision(null);
    }
  }

  async function loadApprovalsForSession(sessionId: string) {
    const approvalsResponse = await fetch("/api/kernel/approvals");
    if (!approvalsResponse.ok) return;
    const allPending: Approval[] = await approvalsResponse.json();
    const sessionPending = allPending.filter((item) => item.session_id === sessionId);
    const latestRunId = sessionPending
      .slice()
      .sort((left, right) =>
        String(right.created_at || "").localeCompare(String(left.created_at || "")),
      )[0]?.run_id;
    setApprovals(
      latestRunId
        ? sessionPending.filter((item) => item.run_id === latestRunId)
        : sessionPending,
    );
  }

  async function openSession(sessionId: string) {
    const response = await fetch(`/api/kernel/sessions/${sessionId}`);
    if (!response.ok) return;
    const session: SessionSummary = await response.json();
    await fetch(`/api/kernel/crons/runs/read-session/${sessionId}`, { method: "POST" });
    if (session.status === "approval_pending") {
      await loadApprovalsForSession(sessionId);
    } else {
      setApprovals([]);
    }
    setUnreadSessionIds((current) => {
      if (!current.has(sessionId)) return current;
      const updated = new Set(current);
      updated.delete(sessionId);
      return updated;
    });
    setActiveSessionId(sessionId);
    setAgentId(session.agent_id);
    setTraceEvents(session.events || []);
    setActiveRunId(null);
    const restored: Message[] = session.messages?.length
      ? session.messages.map((message, index) => ({
          id: `${sessionId}-${index}`,
          role: message.role,
          content: message.content,
          meta: message.role === "user"
            ? session.agent_id
            : `${session.status} · session ${sessionId.slice(0, 8)}`,
          error: message.error,
          runId: message.run_id,
          artifacts: message.artifacts,
        }))
      : [{
          id: `${sessionId}-prompt`,
          role: "user",
          content: session.prompt,
          meta: session.agent_id,
        }];
    setMessages(restored);
  }

  function newConversation() {
    if (running) return;
    setActiveSessionId(null);
    setMessages([]);
    setTraceEvents([]);
    setActiveRunId(null);
    setApprovals([]);
    setPrompt("");
    textarea.current?.focus();
  }

  function startEventStream(sessionId: string) {
    const source = new EventSource(`/api/kernel/sessions/${sessionId}/events`);
    source.addEventListener("trace", (raw) => {
      const event = JSON.parse((raw as MessageEvent).data) as TraceEvent;
      if (event.type === "session.started" && !event.parent_run_id) {
        setActiveRunId(event.run_id);
      }
      setTraceEvents((current) => {
        const key = `${event.timestamp}:${event.type}:${event.run_id}`;
        return current.some((item) => `${item.timestamp}:${item.type}:${item.run_id}` === key)
          ? current : [...current, event];
      });
    });
    return source;
  }

  function toggleSkill(name: string) {
    setSelectedSkills((current) =>
      current.includes(name)
        ? current.filter((skill) => skill !== name)
        : [...current, name],
    );
  }

  function chooseWorkspace(path: string) {
    setActiveWorkspace(path);
    window.localStorage.setItem("amk.activeWorkspace", path);
  }

  function registerWorkspace(workspace: Workspace) {
    const next = [workspace, ...workspaces.filter((item) => item.path !== workspace.path)];
    setWorkspaces(next);
    chooseWorkspace(workspace.path);
    setWorkspaceInput("");
    window.localStorage.setItem("amk.workspaces", JSON.stringify(next));
  }

  async function addWorkspace(event: FormEvent) {
    event.preventDefault();
    const path = workspaceInput.trim();
    if (!path) return;
    setWorkspaceError("");
    const response = await fetch("/api/kernel/workspaces/validate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await response.json();
    if (!response.ok) {
      setWorkspaceError(
        response.status === 404 && data.detail === "Not Found"
          ? "API workspace indisponible — redémarre `amk serve`."
          : data.detail || "Dossier invalide",
      );
      return;
    }
    registerWorkspace(data as Workspace);
  }

  async function pickWorkspace() {
    setPickingWorkspace(true);
    setWorkspaceError("");
    try {
      const response = await fetch("/api/kernel/workspaces/pick", { method: "POST" });
      const data = await response.json();
      if (response.status === 409) return;
      if (!response.ok) throw new Error(data.detail || "Sélecteur de dossier indisponible");
      registerWorkspace(data as Workspace);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Sélecteur de dossier indisponible");
    } finally {
      setPickingWorkspace(false);
    }
  }

  function forgetWorkspace(path: string) {
    if (workspaces.length <= 1) return;
    const next = workspaces.filter((item) => item.path !== path);
    setWorkspaces(next);
    window.localStorage.setItem("amk.workspaces", JSON.stringify(next));
    if (activeWorkspace === path && next[0]) chooseWorkspace(next[0].path);
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const content = prompt.trim();
    if (!content || running) return;
    const secretCommand = content.match(/^\/secret\s+(\S+)\s+[\s\S]+$/i);
    const displayedContent = secretCommand
      ? `/secret ${secretCommand[1]} ••••••••`
      : content;
    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: displayedContent,
      meta: `${agentId}${selectedSkills.length ? ` · ${selectedSkills.length} skill${selectedSkills.length > 1 ? "s" : ""}` : ""}`,
    };
    const sessionId = activeSessionId || crypto.randomUUID();
    const continuingSession = Boolean(activeSessionId);
    runningSessionId.current = sessionId;
    setStopRequested(false);
    setActiveSessionId(sessionId);
    if (!continuingSession) setTraceEvents([]);
    setActiveRunId(null);
    setMessages((current) => continuingSession ? [...current, userMessage] : [userMessage]);
    setPrompt("");
    setRunning(true);
    const eventSource = startEventStream(sessionId);
    try {
      const response = await fetch("/api/kernel/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          prompt: content,
          agent_id: agentId,
          skills: selectedSkills,
          workspace: activeWorkspace || undefined,
          session_id: sessionId,
          security_mode: securityMode,
          provider_id: providerId || undefined,
          model: selectedModel || undefined,
          reasoning,
          images: composerImages.map((image) => ({
            name: image.name,
            media_type: image.mediaType,
            data_base64: image.dataUrl.split(",", 2)[1],
          })),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Le kernel a refusé la requête.");
      const failed = data.status === "failed" || data.status === "timeout";
      const answer =
        data.output ||
        data.errors?.map((error: { message: string }) => error.message).join("\n") ||
        (data.status === "cancelled" ? "Run arrêté." : "Aucun résultat retourné.");
      if (data.status === "approval_pending") {
        await loadApprovalsForSession(data.session_id);
      } else {
        setMessages((current) => [
          ...current,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: answer,
            meta: failed
              ? data.status
              : `${activeAgent?.model || "modèle par défaut"} · session ${data.session_id.slice(0, 8)}`,
            error: failed,
            runId: data.run_id,
            artifacts: data.artifacts,
          },
        ]);
      }
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: error instanceof Error ? error.message : "Connexion impossible.",
          meta: "erreur de connexion",
          error: true,
          runId: activeRunId || undefined,
        },
      ]);
    } finally {
      eventSource.close();
      await refreshSessions();
      await refreshPlan(sessionId);
      setRunning(false);
      runningSessionId.current = null;
      setStopRequested(false);
      setComposerImages([]);
      textarea.current?.focus();
    }
  }

  async function resolveApproval(approval: Approval, approved: boolean) {
    const previousApprovals = approvals;
    setApprovals((current) => current.filter((item) => item.approval_id !== approval.approval_id));
    setApprovalProgress(approved ? "Autorisation enregistrée · reprise en cours" : "Refus enregistré · reprise en cours");
    setRunning(true);
    runningSessionId.current = approval.session_id;
    const eventSource = startEventStream(approval.session_id);
    try {
      const response = await fetch(`/api/kernel/approvals/${approval.approval_id}/resolve`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ approved }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Résolution impossible.");
      if (data.status === "approval_pending") {
        const pendingResponse = await fetch("/api/kernel/approvals");
        if (pendingResponse.ok) {
          const pending: Approval[] = await pendingResponse.json();
          setApprovals(pending.filter((item) => item.session_id === approval.session_id));
        }
      } else {
        setMessages((current) => [
          ...current,
          {
            id: crypto.randomUUID(), role: "assistant",
            content: data.output || data.errors?.map((error: { message: string }) => error.message).join("\n") ||
              (approved ? "Action autorisée." : "Action refusée."),
            meta: data.status,
            error: data.status === "failed" || data.status === "timeout",
            runId: data.run_id,
            artifacts: data.artifacts,
          },
        ]);
      }
    } catch (error) {
      setApprovals(previousApprovals);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(), role: "assistant", error: true,
          content: error instanceof Error ? error.message : "Connexion impossible.",
          meta: "guardian",
        },
      ]);
    } finally {
      eventSource.close();
      setApprovalProgress("");
      await refreshSessions();
      setRunning(false);
      runningSessionId.current = null;
    }
  }

  async function resolveApprovalBatch(approved: boolean) {
    if (approvals.length === 0) return;
    const previousApprovals = approvals;
    setRunning(true);
    const sessionId = approvals[0].session_id;
    runningSessionId.current = sessionId;
    setApprovals([]);
    setApprovalProgress(approved
      ? `${previousApprovals.length} autorisations enregistrées · reprise en cours`
      : `${previousApprovals.length} refus enregistrés · reprise en cours`);
    const eventSource = startEventStream(sessionId);
    try {
      const response = await fetch("/api/kernel/approvals/resolve-batch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          approval_ids: previousApprovals.map((item) => item.approval_id),
          approved,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Résolution impossible.");
      if (data.status === "approval_pending") {
        const pendingResponse = await fetch("/api/kernel/approvals");
        if (pendingResponse.ok) {
          const pending: Approval[] = await pendingResponse.json();
          setApprovals(pending.filter((item) => item.session_id === sessionId));
        }
      } else {
        setMessages((current) => [
          ...current,
          {
            id: crypto.randomUUID(), role: "assistant",
            content: data.output || data.errors?.map((error: { message: string }) => error.message).join("\n") ||
              (approved ? "Actions autorisées." : "Actions refusées."),
            meta: data.status,
            error: data.status === "failed" || data.status === "timeout",
          },
        ]);
      }
    } catch (error) {
      setApprovals(previousApprovals);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(), role: "assistant", error: true, meta: "guardian",
          content: error instanceof Error ? error.message : "Connexion impossible.",
        },
      ]);
    } finally {
      eventSource.close();
      setApprovalProgress("");
      await refreshSessions();
      setRunning(false);
      runningSessionId.current = null;
    }
  }

  const cronApprovalGroups = groupRoutineApprovals(cronTestApprovals);
  const persistedCron = cronEditor && !cronEditor.creating
    ? cronJobs.find((job) => job.id === cronEditor.id)
    : null;
  const cronPermissionConfigDirty = Boolean(cronEditor && persistedCron && (
    cronEditor.prompt !== persistedCron.prompt
    || cronEditor.workspace !== persistedCron.workspace
    || cronEditor.agent_id !== persistedCron.agent_id
    || JSON.stringify(cronEditor.skills) !== JSON.stringify(persistedCron.skills)
    || cronEditor.security_mode !== persistedCron.security_mode
    || cronEditor.provider_id !== persistedCron.provider_id
    || cronEditor.model !== persistedCron.model
    || cronEditor.reasoning !== persistedCron.reasoning
  ));
  const cronWorkflowBasisDirty = Boolean(cronEditor && persistedCron && (
    cronEditor.name.trim() !== persistedCron.name
    || scheduleFromEditor(cronEditor) !== persistedCron.schedule
    || cronEditor.prompt !== persistedCron.prompt
    || cronEditor.workspace !== persistedCron.workspace
    || cronEditor.agent_id !== persistedCron.agent_id
    || JSON.stringify([...cronEditor.skills].sort())
      !== JSON.stringify([...persistedCron.skills].sort())
    || cronEditor.security_mode !== persistedCron.security_mode
  ));
  const workflowCreatorAvailable = Boolean(
    catalog?.skills.some((skill) => skill.name === "workflow-creator"),
  );
  const cronWorkflowProposalReady = cronWorkflowProposal?.workflow.status === "ready";
  const cronWorkflowMutationAvailable = Boolean(
    cronEditor
    && !cronEditor.creating
    && !cronEditor.in_flight
    && !cronEditor.blocked
    && !testingCron
    && !savingResource
    && !cronApprovalStatus?.pending_count,
  );
  const cronWorkflowMutationBusy = cronWorkflowAction === "accept"
    || cronWorkflowAction === "delete";
  const cronWorkflowCanAccept = Boolean(
    cronEditor
    && cronWorkflowProposal
    && !cronEditor.in_flight
    && !cronEditor.blocked
    && !testingCron
    && !savingResource
    && !cronWorkflowMutationBusy
    && (!cronEditor.enabled || cronWorkflowProposalReady),
  );
  const cronWorkflowCanPropose = Boolean(
    cronEditor
    && workflowCreatorAvailable
    && cronEditor.name.trim()
    && cronEditor.prompt.trim()
    && !savingResource,
  );
  const cronCanSave = Boolean(
    cronEditor
    && cronEditor.name.trim()
    && cronEditor.prompt.trim()
    && !savingResource
    && !testingCron
    && !cronWorkflowAction
    && !cronWorkflowProposal
    && !(cronEditor.workflow && cronWorkflowBasisDirty)
    && !(cronEditor.workflow && cronEditor.enabled && cronEditor.workflow.status !== "ready"),
  );
  const cronWorkflowCanContinue = Boolean(
    cronEditor
    && cronEditor.name.trim()
    && cronEditor.prompt.trim()
    && !savingResource
    && !testingCron
    && !cronWorkflowAction,
  );

  return (
    <main className="shell">
      <aside className="rail">
        <header className="rail-head">
          <div className="brand">
            <span className="brand-mark">A</span>
            <div>
              <strong>AMK</strong>
              <small>Agentic kernel</small>
            </div>
          </div>
        </header>

        <div className="rail-content" ref={railContent}>
          <ResourceNavigation
            projectName={activeWorkspaceInfo?.name}
            projectPath={activeWorkspaceInfo?.path}
            agentId={activeAgent?.id}
            agentProvider={activeAgent?.provider}
            selectedSkillCount={selectedSkills.length}
            availableSkillCount={catalog?.skills.length || 0}
            providerCount={catalog?.providers.length || 0}
            defaultProvider={catalog?.default_provider}
            activeCronCount={cronJobs.filter((job) => job.enabled).length}
            unreadCronCount={cronRuns.filter((item) => item.delivery_status === "unread").length}
            onOpen={setManagementModal}
          />

          <SessionHistory
            sessions={sessions}
            activeSessionId={activeSessionId}
            unreadSessionIds={unreadSessionIds}
            running={running}
            onOpen={(sessionId) => void openSession(sessionId)}
            onResume={(sessionId) => void resumeSession(sessionId)}
            onDelete={(sessionId) => void deleteSession(sessionId)}
          />
        </div>

        <footer className="rail-footer">
          <div className={`kernel-status ${catalogError ? "offline" : ""}`}>
            <span />
            <div>
              <strong>{catalogError ? "Kernel hors ligne" : "Kernel connecté"}</strong>
              <small>{catalogError || catalog?.default_provider || "connexion…"}</small>
            </div>
          </div>
        </footer>
      </aside>

      {managementModal && (
        <div className="management-backdrop" onMouseDown={() => setManagementModal(null)}>
          <section
            className="management-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="management-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header>
              <div>
                <p className="eyebrow">Configuration du contexte</p>
                <h2 id="management-title">
                  {managementModal === "projects" && "Projets"}
                  {managementModal === "agents" && "Agents"}
                  {managementModal === "skills" && "Skills"}
                  {managementModal === "providers" && "Providers"}
                  {managementModal === "crons" && "Automatisations"}
                </h2>
              </div>
              <div className="modal-header-actions">
                {(managementModal === "agents" || managementModal === "skills" || managementModal === "providers" || managementModal === "crons")
                  && !resourceEditor && !providerEditor && !cronEditor && (
                  <button
                    className="modal-add"
                    onClick={() => managementModal === "providers"
                      ? createProvider()
                      : managementModal === "crons"
                        ? createCron()
                        : createResource(managementModal)}
                    aria-label="Ajouter"
                  >+</button>
                )}
                <button onClick={() => {
                  setManagementModal(null);
                  setResourceEditor(null);
                  setProviderEditor(null);
                  closeCronEditor();
                }}
                  disabled={Boolean(
                    cronEditor && (cronWorkflowMutationBusy || savingResource || testingCron)
                  )}
                  aria-label="Fermer">×</button>
              </div>
            </header>

            {managementError && (
              <div className="management-error" role="alert">{managementError}</div>
            )}

            {resourceEditor ? (
              <div className="resource-editor">
                <div className="resource-fields">
                  <label>
                    Identifiant
                    <input
                      value={resourceEditor.id}
                      disabled={!resourceEditor.creating}
                      onChange={(event) => setResourceEditor((current) => current && ({
                        ...current, id: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="field-wide">
                    Description
                    <input
                      value={String(resourceEditor.frontmatter.description || "")}
                      onChange={(event) => updateEditorField("description", event.target.value)}
                    />
                  </label>
                  {resourceEditor.kind === "agents" ? (
                    <>
                      <label>
                        Provider
                        <select
                          value={String(resourceEditor.frontmatter.provider || "")}
                          onChange={(event) => {
                            updateEditorField("provider", event.target.value);
                            updateEditorField("model", "");
                          }}
                        >
                          {catalog?.providers.map((provider) => (
                            <option key={provider.id} value={provider.id}>{provider.id}</option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Modèle
                        <select
                          value={String(resourceEditor.frontmatter.model || "")}
                          onChange={(event) => updateEditorField("model", event.target.value || null)}
                        >
                          <option value="">
                            {modelsLoading ? "Découverte…" : "Modèle par défaut du provider"}
                          </option>
                          {Array.from(new Set([
                            ...(providerModels[String(resourceEditor.frontmatter.provider || "")] || []),
                            String(resourceEditor.frontmatter.model || ""),
                          ].filter(Boolean))).map((model) => (
                            <option key={model} value={model}>{model}</option>
                          ))}
                        </select>
                      </label>
                      <fieldset className="field-wide checkbox-field">
                        <legend>Tools actifs</legend>
                        <div className="checkbox-grid">
                          {catalog?.tools.map((tool) => (
                            <label key={tool.name} title={tool.description}>
                              <input
                                type="checkbox"
                                checked={
                                  !Object.prototype.hasOwnProperty.call(resourceEditor.frontmatter, "tools")
                                  || resourceList(resourceEditor.frontmatter.tools).includes(tool.name)
                                }
                                onChange={() => toggleEditorListField(
                                  "tools",
                                  tool.name,
                                  catalog?.tools.map((item) => item.name) || [],
                                )}
                              />
                              <span><strong>{tool.name}</strong><small>{tool.module}</small></span>
                            </label>
                          ))}
                        </div>
                      </fieldset>
                      <fieldset className="field-wide checkbox-field">
                        <legend>Skills préchargées</legend>
                        <div className="checkbox-grid">
                          {catalog?.skills.map((skill) => (
                            <label key={skill.name} title={skill.description}>
                              <input
                                type="checkbox"
                                checked={resourceList(resourceEditor.frontmatter.skills).includes(skill.name)}
                                onChange={() => toggleEditorListField("skills", skill.name)}
                              />
                              <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
                            </label>
                          ))}
                          {catalog?.skills.length === 0 && <p>Aucune skill installée.</p>}
                        </div>
                      </fieldset>
                      <fieldset className="field-wide checkbox-field">
                        <legend>Agents enfants</legend>
                        <div className="checkbox-field-actions">
                          <button
                            type="button"
                            onClick={() => updateEditorField(
                              "delegates",
                              catalog?.agents.map((agent) => agent.id)
                                .filter((id) => id !== resourceEditor.id) || [],
                            )}
                          >Tout sélectionner</button>
                          <button type="button" onClick={() => updateEditorField("delegates", [])}>
                            Aucun
                          </button>
                        </div>
                        <div className="checkbox-grid">
                          {catalog?.agents
                            .filter((agent) => agent.id !== resourceEditor.id)
                            .map((agent) => (
                              <label key={agent.id} title={agent.description}>
                                <input
                                  type="checkbox"
                                  checked={resourceList(resourceEditor.frontmatter.delegates).includes(agent.id)}
                                  onChange={() => toggleEditorListField("delegates", agent.id)}
                                />
                                <span><strong>{agent.id}</strong><small>{agent.provider}</small></span>
                              </label>
                            ))}
                        </div>
                      </fieldset>
                    </>
                  ) : (
                    <fieldset className="field-wide checkbox-field">
                      <legend>Outils autorisés</legend>
                      <div className="checkbox-grid">
                        {catalog?.tools.map((tool) => (
                          <label key={tool.name} title={tool.description}>
                            <input
                              type="checkbox"
                              checked={resourceList(resourceEditor.frontmatter["allowed-tools"]).includes(tool.name)}
                              onChange={() => toggleEditorListField("allowed-tools", tool.name)}
                            />
                            <span><strong>{tool.name}</strong><small>{tool.module}</small></span>
                          </label>
                        ))}
                      </div>
                    </fieldset>
                  )}
                </div>
                <label className="resource-body-field">
                  {resourceEditor.kind === "agents" ? "Instructions de l’agent" : "Instructions de la skill"}
                  <textarea
                    value={resourceEditor.body}
                    onChange={(event) => setResourceEditor((current) => current && ({
                      ...current, body: event.target.value,
                    }))}
                    spellCheck
                  />
                </label>
                <div className="resource-editor-actions">
                  <button onClick={() => setResourceEditor(null)}>Annuler</button>
                  <button
                    className="primary"
                    disabled={savingResource || !resourceEditor.id.trim() || !resourceEditor.body.trim()}
                    onClick={() => void saveResource()}
                  >
                    {savingResource ? "Enregistrement…" : "Enregistrer"}
                  </button>
                </div>
              </div>
            ) : managementModal === "projects" ? (
              <div className="management-body">
                <form className="workspace-form modal-workspace-form" onSubmit={addWorkspace}>
                  <input
                    value={workspaceInput}
                    onChange={(event) => setWorkspaceInput(event.target.value)}
                    placeholder="/chemin/du/projet"
                    aria-label="Chemin du projet"
                  />
                  <button
                    type="button"
                    className="pick-workspace"
                    disabled={pickingWorkspace}
                    onClick={() => void pickWorkspace()}
                    aria-label="Choisir un dossier"
                    title="Choisir un dossier"
                  >
                    {pickingWorkspace ? "…" : (
                      <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M3.5 6.5h6l2 2h9v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />
                      </svg>
                    )}
                  </button>
                  <button className="add-workspace" disabled={!workspaceInput.trim()}>+</button>
                </form>
                {workspaceError && <small className="workspace-error">{workspaceError}</small>}
                <div className="management-list">
                  {workspaces.map((workspace) => (
                    <div className={`management-row ${workspace.path === activeWorkspace ? "active" : ""}`} key={workspace.path}>
                      <button onClick={() => { chooseWorkspace(workspace.path); setManagementModal(null); }}>
                        <span className="row-status" />
                        <span>
                          <strong>{workspace.name}</strong>
                          <small>{workspace.path}</small>
                        </span>
                        {workspace.path === activeWorkspace && <em>Actif</em>}
                      </button>
                      <button
                        className="management-remove"
                        onClick={() => forgetWorkspace(workspace.path)}
                        disabled={workspaces.length <= 1}
                        aria-label={`Oublier ${workspace.name}`}
                      >×</button>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {!resourceEditor && managementModal === "agents" && (
              <div className="management-body management-list">
                {managedResources.map((resource) => (
                  <div className={`resource-row ${resource.id === agentId ? "active" : ""}`} key={resource.id}>
                    <button
                      className="resource-select"
                      onClick={() => { setAgentId(resource.id); setManagementModal(null); }}
                    >
                      <span className="context-icon agent-icon">{resource.id.slice(0, 1).toUpperCase()}</span>
                      <span>
                        <strong>{resource.id}</strong>
                        <small>{resource.description}</small>
                      </span>
                    </button>
                    <div className="resource-actions">
                      <button
                        onClick={() => editResource("agents", resource)}
                        aria-label={`Éditer ${resource.id}`}
                      >✎</button>
                      <button
                        disabled={resource.id === "main"}
                        onClick={() => void deleteResource("agents", resource.id)}
                        aria-label={`Supprimer ${resource.id}`}
                      >⌫</button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {!resourceEditor && managementModal === "skills" && (
              <div className="management-body management-list">
                {managedResources.map((resource) => (
                  <div className={`resource-row ${selectedSkills.includes(resource.id) ? "active" : ""}`} key={resource.id}>
                    <label className="resource-select">
                      <input
                        type="checkbox"
                        checked={selectedSkills.includes(resource.id)}
                        onChange={() => toggleSkill(resource.id)}
                      />
                      <span>
                        <strong>{resource.id}</strong>
                        <small>{resource.description}</small>
                      </span>
                    </label>
                    <div className="resource-actions">
                      <button
                        onClick={() => editResource("skills", resource)}
                        aria-label={`Éditer ${resource.id}`}
                      >✎</button>
                      <button
                        onClick={() => void deleteResource("skills", resource.id)}
                        aria-label={`Supprimer ${resource.id}`}
                      >⌫</button>
                    </div>
                  </div>
                ))}
                {managedResources.length === 0 && (
                  <div className="management-empty">
                    <strong>Aucun skill installé</strong>
                    <p>Ajoute les skills dans <code>content-agents/skills/</code>.</p>
                  </div>
                )}
              </div>
            )}

            {managementModal === "providers" && providerEditor && (
              <div className="resource-editor provider-editor">
                <div className="resource-fields">
                  <label>
                    Identifiant
                    <input
                      value={providerEditor.id}
                      disabled={!providerEditor.creating}
                      onChange={(event) => setProviderEditor((current) => current && ({
                        ...current, id: event.target.value,
                      }))}
                    />
                  </label>
                  <label>
                    Type
                    <select
                      value={providerEditor.kind}
                      onChange={(event) => selectProviderKind(event.target.value)}
                    >
                      {["deepseek", "llama-cpp", "openai-codex", "claude-oauth", "openai", "anthropic"].map((kind) => (
                        <option key={kind} value={kind}>
                          {providerKindLabels[kind] || kind}
                        </option>
                      ))}
                    </select>
                  </label>
                  {providerEditor.kind === "openai-codex" ? (
                    <>
                      <label>
                        Connexion
                        <input value="OAuth ChatGPT" disabled />
                      </label>
                      <label className="field-wide">
                        Compte ChatGPT
                        <div className="oauth-connect-card">
                          <span className={codexConnected ? "connected" : ""} />
                          <div>
                            <strong>
                              {codexConnected ? "Codex connecté" : "Connexion requise"}
                            </strong>
                            <small>
                              {codexConnected
                                ? "Les modèles sont découverts automatiquement."
                                : "Le navigateur va ouvrir la page de connexion OpenAI."}
                            </small>
                          </div>
                          <button
                            type="button"
                            className={codexConnected ? "" : "primary"}
                            disabled={codexAuthLoading}
                            onClick={() => void (
                              codexConnected ? disconnectCodex() : connectCodex()
                            )}
                          >
                            {codexAuthLoading
                              ? "Connexion…"
                              : codexConnected ? "Se déconnecter" : "Se connecter avec ChatGPT"}
                          </button>
                        </div>
                        <a
                          className="oauth-help-link"
                          href={providerEditor.auth_help_url || codexAuthHelpUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Consulter la procédure officielle OpenAI Codex ↗
                        </a>
                        <small>
                          Le jeton est conservé dans le trousseau système, jamais dans providers.json.
                        </small>
                      </label>
                    </>
                  ) : (
                    <>
                      <label>
                        Connexion
                        <select
                          value={providerEditor.connection_type || "api_key"}
                          onChange={(event) => setProviderEditor((current) => current && ({
                            ...(current.kind === "openai" && event.target.value === "auth"
                              ? {
                                  ...current,
                                  kind: "openai-codex",
                                  connection_type: "auth" as const,
                                  base_url: codexApiUrl,
                                  model: null,
                                  models: [],
                                  timeout_seconds: undefined,
                                  auth_help_url: codexAuthHelpUrl,
                                }
                              : {
                                  ...current,
                                  connection_type: event.target.value as ManagedProvider["connection_type"],
                                }),
                          }))}
                        >
                          <option value="local">local</option>
                          <option value="api_key">api_key</option>
                          <option value="auth">auth</option>
                        </select>
                      </label>
                      <label>
                        URL de base
                        <input
                          value={providerEditor.base_url || ""}
                          onChange={(event) => setProviderEditor((current) => current && ({
                            ...current, base_url: event.target.value || null,
                          }))}
                        />
                      </label>
                      <label>
                        Modèle par défaut
                        <input
                          value={providerEditor.model || ""}
                          onChange={(event) => setProviderEditor((current) => current && ({
                            ...current, model: event.target.value,
                          }))}
                        />
                      </label>
                      <label>
                        Modèles configurés
                        <input
                          value={(providerEditor.models || []).join(", ")}
                          onChange={(event) => setProviderEditor((current) => current && ({
                            ...current, models: parseListFieldValue(event.target.value),
                          }))}
                          placeholder="model-a, model-b"
                        />
                      </label>
                    </>
                  )}
                  {providerEditor.connection_type === "api_key" && (
                    <label className="field-wide">
                      Clé API
                      <input
                        type="password"
                        value={providerEditor.api_key || ""}
                        placeholder={providerEditor.api_key_configured
                          ? "Clé configurée — laisser vide pour la conserver"
                          : "Clé API"}
                        onChange={(event) => setProviderEditor((current) => current && ({
                          ...current, api_key: event.target.value,
                        }))}
                      />
                    </label>
                  )}
                  {providerEditor.kind !== "openai-codex" && (
                    <label>
                      Timeout
                      <input
                        type="number"
                        min={1}
                        value={providerEditor.timeout_seconds || 120}
                        onChange={(event) => setProviderEditor((current) => current && ({
                          ...current, timeout_seconds: Number(event.target.value),
                        }))}
                      />
                    </label>
                  )}
                  <label className="provider-vision">
                    <input
                      type="checkbox"
                      checked={Boolean(providerEditor.vision)}
                      onChange={(event) => setProviderEditor((current) => current && ({
                        ...current, vision: event.target.checked,
                      }))}
                    />
                    Modèle avec vision
                  </label>
                </div>
                <div className="resource-editor-actions">
                  <button onClick={() => setProviderEditor(null)}>Annuler</button>
                  <button
                    className="primary"
                    disabled={
                      savingResource
                      || !providerEditor.id
                      || (providerEditor.kind !== "openai-codex" && !providerEditor.model)
                    }
                    onClick={() => void saveProvider()}
                  >
                    {savingResource ? "Enregistrement…" : "Enregistrer"}
                  </button>
                </div>
              </div>
            )}

            {managementModal === "providers" && !providerEditor && (
              <div className="management-body management-list">
                {managedProviders.map((provider) => (
                  <div className="resource-row" key={provider.id}>
                    <button
                      className="resource-select"
                      onClick={() => setProviderEditor({ ...provider, creating: false })}
                    >
                      <span className="context-icon">{provider.connection_type === "local" ? "L" : "P"}</span>
                      <span>
                        <strong>{provider.id}</strong>
                        <small>
                          {provider.kind} · {provider.model || "modèles découverts après connexion"}
                        </small>
                      </span>
                    </button>
                    <div className="resource-actions">
                      {provider.id === defaultProvider && <span className="default-chip">Défaut</span>}
                      <button
                        onClick={() => setProviderEditor({ ...provider, creating: false })}
                        aria-label={`Éditer ${provider.id}`}
                      >✎</button>
                      <button
                        disabled={provider.id === defaultProvider}
                        onClick={() => void deleteProvider(provider.id)}
                        aria-label={`Supprimer ${provider.id}`}
                      >⌫</button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {managementModal === "crons" && cronEditor && (
              <div className="resource-editor cron-editor">
                <fieldset
                  className="resource-fields"
                  disabled={cronWorkflowMutationBusy || savingResource || testingCron}
                >
                  <label className="field-wide">
                    Nom
                    <input value={cronEditor.name} onChange={(event) => {
                      invalidateCronWorkflowProposal();
                      setCronEditor((current) => current && ({ ...current, name: event.target.value }));
                    }}
                    />
                  </label>
                  <label>
                    Répétition
                    <select value={cronEditor.frequency_kind} onChange={(event) => {
                      invalidateCronWorkflowProposal();
                      setCronEditor((current) => current && ({
                        ...current, frequency_kind: event.target.value as CronFrequencyKind,
                      }));
                    }}>
                      <option value="minutes">Toutes les X minutes</option>
                      <option value="hours">Toutes les X heures</option>
                      <option value="daily">Tous les jours</option>
                      <option value="weekly">Toutes les semaines</option>
                      <option value="yearly">Tous les ans</option>
                    </select>
                  </label>
                  {(cronEditor.frequency_kind === "minutes" || cronEditor.frequency_kind === "hours") && (
                    <label>
                      Intervalle
                      <input type="number" min="1"
                        max={cronEditor.frequency_kind === "minutes" ? 59 : 23}
                        value={cronEditor.frequency_interval}
                        onChange={(event) => {
                          invalidateCronWorkflowProposal();
                          setCronEditor((current) => current && ({
                            ...current, frequency_interval: Math.max(1, Number(event.target.value)),
                          }));
                        }}
                      />
                    </label>
                  )}
                  {cronEditor.frequency_kind === "weekly" && (
                    <label>
                      Jour
                      <select value={cronEditor.frequency_weekday} onChange={(event) => {
                        invalidateCronWorkflowProposal();
                        setCronEditor((current) => current && ({
                          ...current, frequency_weekday: Number(event.target.value),
                        }));
                      }}>
                        {["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"]
                          .map((day, index) => <option value={index} key={day}>{day}</option>)}
                      </select>
                    </label>
                  )}
                  {cronEditor.frequency_kind === "yearly" && (
                    <>
                      <label>
                        Mois
                        <select value={cronEditor.frequency_month} onChange={(event) => {
                          invalidateCronWorkflowProposal();
                          setCronEditor((current) => current && ({
                            ...current, frequency_month: Number(event.target.value),
                          }));
                        }}>
                          {["Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
                            "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
                            .map((month, index) => <option value={index + 1} key={month}>{month}</option>)}
                        </select>
                      </label>
                      <label>
                        Jour du mois
                        <input type="number" min="1" max="31" value={cronEditor.frequency_monthday}
                          onChange={(event) => {
                            invalidateCronWorkflowProposal();
                            setCronEditor((current) => current && ({
                              ...current, frequency_monthday: Math.min(31, Math.max(1, Number(event.target.value))),
                            }));
                          }}
                        />
                      </label>
                    </>
                  )}
                  {["daily", "weekly", "yearly"].includes(cronEditor.frequency_kind) && (
                    <label>
                      Heure
                      <input type="time" value={cronEditor.frequency_time} onChange={(event) => {
                        invalidateCronWorkflowProposal();
                        setCronEditor((current) => current && ({
                          ...current, frequency_time: event.target.value,
                        }));
                      }}
                      />
                    </label>
                  )}
                  <div className="field-wide cron-summary">
                    <span>Prochaine règle</span>
                    <strong>{describeCron(cronEditor)}</strong>
                  </div>
                  <label>
                    Agent
                    <select value={cronEditor.agent_id} onChange={(event) => {
                      invalidateCronWorkflowProposal();
                      setCronEditor((current) => current && ({
                        ...current, agent_id: event.target.value, security_mode: securityMode,
                      }));
                    }}>
                      {catalog?.agents.map((agent) => (
                        <option value={agent.id} key={agent.id}>{agent.id}</option>
                      ))}
                    </select>
                  </label>
                  <div className="permission-inherited">
                    <span>Permissions héritées de l’agent actif</span>
                    <strong>{cronEditor.security_mode}</strong>
                  </div>
                  <fieldset className="field-wide checkbox-field cron-skills-field">
                    <legend>Skills de la routine</legend>
                    <p>
                      Ces skills restent actives avec ou sans workflow. Aucune sélection signifie que
                      l’agent suit seulement le prompt.
                    </p>
                    <div className="checkbox-grid">
                      {catalog?.skills
                        .filter((skill) => skill.name !== "workflow-creator")
                        .map((skill) => (
                          <label key={skill.name} title={skill.description}>
                            <input
                              type="checkbox"
                              checked={cronEditor.skills.includes(skill.name)}
                              onChange={() => {
                                invalidateCronWorkflowProposal();
                                setCronEditor((current) => current && ({
                                  ...current,
                                  skills: current.skills.includes(skill.name)
                                    ? current.skills.filter((item) => item !== skill.name)
                                    : [...current.skills, skill.name],
                                }));
                              }}
                            />
                            <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
                          </label>
                        ))}
                      {catalog?.skills.filter((skill) => skill.name !== "workflow-creator").length === 0 && (
                        <p>Aucune skill d’exécution installée.</p>
                      )}
                    </div>
                  </fieldset>
                  <label className="field-wide">
                    Workspace
                    <input value={cronEditor.workspace} onChange={(event) => {
                      invalidateCronWorkflowProposal();
                      setCronEditor((current) => current && ({ ...current, workspace: event.target.value }));
                    }}
                    />
                  </label>
                  <label className="field-wide">
                    Résultats envoyés dans
                    <select value={cronEditor.notification_session_id} onChange={(event) =>
                      setCronEditor((current) => current && ({
                        ...current, notification_session_id: event.target.value,
                      }))}>
                      {sessions.map((session) => (
                        <option value={session.session_id} key={session.session_id}>
                          {session.trigger === "routine_inbox"
                            ? "Routines (par défaut)"
                            : session.prompt || "Session sans titre"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field-wide">
                    Demande exécutée
                    <textarea value={cronEditor.prompt} onChange={(event) => {
                      invalidateCronWorkflowProposal();
                      setCronEditor((current) => current && ({ ...current, prompt: event.target.value }));
                    }}
                      placeholder="Décris le résultat attendu à chaque exécution…"
                    />
                  </label>
                  <div className="field-wide switch-setting">
                    <span><strong>Routine active</strong><small>Exécuter selon la fréquence choisie</small></span>
                    <button type="button" role="switch" aria-checked={cronEditor.enabled}
                      className={`toggle-switch ${cronEditor.enabled ? "on" : ""}`}
                      onClick={() => setCronEditor((current) => current && ({
                        ...current, enabled: !current.enabled,
                      }))}><span /></button>
                  </div>
                  <div className="field-wide switch-setting">
                    <span><strong>Reprise après échec</strong><small>Continuer au prochain passage après une erreur transitoire</small></span>
                    <button type="button" role="switch" aria-checked={cronEditor.auto_resume}
                      className={`toggle-switch ${cronEditor.auto_resume ? "on" : ""}`}
                      onClick={() => setCronEditor((current) => current && ({
                        ...current, auto_resume: !current.auto_resume,
                      }))}><span /></button>
                  </div>
                </fieldset>
                <RoutineWorkflowPanel
                  workflow={cronEditor.workflow || null}
                  proposal={cronWorkflowProposal}
                  revision={cronEditor.workflow_revision}
                  updatedAt={cronEditor.workflow_updated_at}
                  action={cronWorkflowAction}
                  feedback={
                    cronEditor.workflow && cronWorkflowBasisDirty && !cronWorkflowProposal
                      ? "warning"
                      : cronWorkflowFeedback
                  }
                  message={
                    cronWorkflowProposal && cronEditor.enabled && !cronWorkflowProposalReady
                      ? "Ce workflow n’est pas prêt. Désactive la routine pour l’enregistrer comme brouillon, ou corrige ses dépendances."
                      : cronWorkflowProposal && (cronEditor.blocked || cronEditor.in_flight)
                        ? "La routine exécute une occurrence réelle ou attend son autorisation. Termine-la avant de changer son mode d’exécution."
                        : cronWorkflowProposal && Boolean(cronApprovalStatus?.pending_count)
                          ? "Accepter ce workflow remplacera le test du mode libre en attente. Ses autorisations seront annulées et le nouveau workflow devra être testé."
                        : cronEditor.workflow && !cronWorkflowMutationAvailable
                          ? "Le workflow reste consultable. Attends la fin de l’exécution ou de la validation pour le modifier ou le supprimer."
                      : !cronEditor.creating
                      && cronEditor.workflow
                      && cronWorkflowBasisDirty
                      && !cronWorkflowProposal
                      ? "Ces modifications rendent le workflow actif obsolète. Propose une nouvelle version ou supprime le workflow avant d’enregistrer."
                      : cronWorkflowMessage || (!workflowCreatorAvailable
                        ? "Le générateur workflow-creator n’apparaît pas dans l’application active. Redémarre l’application après son installation ; le mode libre reste disponible."
                        : "")
                  }
                  creating={cronEditor.creating}
                  canPropose={cronWorkflowCanPropose}
                  canContinueWithoutWorkflow={cronWorkflowCanContinue}
                  canAcceptProposal={cronWorkflowCanAccept}
                  canDelete={cronWorkflowMutationAvailable}
                  onPropose={() => void proposeCronWorkflow()}
                  onContinueWithoutWorkflow={() => void continueWithoutCronWorkflow()}
                  onAcceptProposal={() => void acceptCronWorkflowChoice()}
                  onDiscardProposal={discardCronWorkflowProposal}
                  onDelete={() => void deleteCronWorkflow()}
                />
                {!cronWorkflowProposal && (
                  <>
                    <p className="cron-note">
                      Teste la routine avant de l’activer. Les demandes ASK validées pendant le test
                      seront mémorisées pour cette routine, uniquement pour l’outil et la cible affichés.
                    </p>
                    {!cronEditor.creating && (
                      <div className={`cron-test-panel ${cronTestFeedback}`} aria-busy={testingCron}>
                    <div className="cron-test-heading">
                      <strong>Validation avant automatisation</strong>
                      {cronApprovalStatus && cronApprovalStatus.approved_scopes.length > 0
                        && !cronPermissionConfigDirty
                        && !testingCron
                        && cronTestApprovals.length === 0 && (
                        <span className="cron-prevalidation-badge">
                          ✓ Prévalidation active
                        </span>
                      )}
                      <small
                        role={cronTestFeedback === "error" ? "alert" : "status"}
                        aria-live="polite"
                      >
                        {testingCron && <span className="cron-test-spinner" aria-hidden="true" />}
                        {cronPermissionConfigDirty
                          ? "Enregistre les modifications avant de relancer la prévalidation."
                          : cronTestMessage || "Lance un test pour détecter les autorisations nécessaires."}
                      </small>
                    </div>
                    {cronTestApprovals.length > 0 && (
                      <ul>
                        {cronApprovalGroups.map((group) => (
                          <li key={group.key}>
                            <strong>{group.tool_name}</strong>
                            <span>
                              {group.count > 1 ? `${group.count} actions prévues · ` : ""}
                              {group.justifications[0]}
                            </span>
                            {group.path && <code>{group.path}</code>}
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="cron-test-actions">
                      {cronTestApprovals.length > 0 ? (
                        <>
                          <button disabled={testingCron || cronPermissionConfigDirty}
                            onClick={() => void resolveCronTestApprovals(false)}>
                            {cronTestDecision === "reject" ? "Refus en cours…" : "Tout refuser"}
                          </button>
                          <button className="primary" disabled={testingCron || cronPermissionConfigDirty}
                            onClick={() => void resolveCronTestApprovals(true)}>
                            {cronTestDecision === "approve"
                              ? "Autorisation en cours…"
                              : "Autoriser cette routine"}
                          </button>
                        </>
                      ) : (
                        <button disabled={testingCron || cronEditor.in_flight || cronPermissionConfigDirty}
                          onClick={() => void testCron(cronEditor.id)}>
                          {testingCron
                            ? "Test en cours…"
                            : cronPermissionConfigDirty
                              ? "Enregistrer avant de tester"
                              : "Tester la routine"}
                        </button>
                      )}
                    </div>
                      </div>
                    )}
                  </>
                )}
                <div className="resource-editor-actions">
                  <button
                    onClick={closeCronEditor}
                    disabled={cronWorkflowMutationBusy || savingResource || testingCron}
                  >Annuler</button>
                  {!cronEditor.creating && cronEditor.workflow && !cronWorkflowProposal && (
                    <button className="primary" onClick={() => void saveCron()} disabled={!cronCanSave}>
                      {savingResource
                        ? "Enregistrement…"
                        : cronEditor.workflow && cronWorkflowBasisDirty
                          ? "Workflow à mettre à jour"
                          : "Enregistrer"}
                    </button>
                  )}
                </div>
              </div>
            )}

            {managementModal === "crons" && !cronEditor && (
              <div className="management-body management-list">
                {cronJobs.map((job) => (
                  <div className={`cron-row ${job.enabled ? "" : "disabled"}`} key={job.id}>
                    <button className="cron-main"
                      onClick={() => void openCronEditor(job)}>
                      <span className={`row-status ${job.in_flight ? "running" : job.last_status || ""}`} />
                      <span>
                        <strong>{job.name}</strong>
                        <small>{describeCron(cronEditorFromJob(job))} · {job.agent_id} · {job.workspace}</small>
                        <em>
                          {job.blocked
                            ? "En attente d’autorisation"
                            : job.in_flight
                            ? "En cours"
                            : job.next_run_at
                              ? `Prochaine exécution ${new Date(job.next_run_at).toLocaleString("fr-FR")}`
                              : "Aucune exécution prévue"}
                        </em>
                        {job.last_error && <b>{job.last_error}</b>}
                      </span>
                    </button>
                    <div className="cron-actions">
                      <button type="button" role="switch" aria-checked={job.enabled}
                        className={`mini-toggle ${job.enabled ? "on" : ""}`}
                        onClick={() => void toggleCron(job, !job.enabled)}
                        title={job.enabled ? "Désactiver" : "Activer"}><span /></button>
                      <button onClick={() => void runCronNow(job.id)} disabled={job.in_flight || !job.enabled}
                        title="Lancer maintenant">▶</button>
                      <button onClick={() => void openCronEditor(job)}
                        title="Éditer">✎</button>
                      <button onClick={() => void deleteCron(job.id)} title="Supprimer">⌫</button>
                    </div>
                  </div>
                ))}
                {cronJobs.length === 0 && (
                  <div className="management-empty">
                    <CalendarClock />
                    <p>Aucune automatisation. Ajoute une routine avec le bouton +.</p>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      )}

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Conversation active</p>
            <h1>{activeAgent?.id || "main"}</h1>
            {activeWorkspace && <small className="active-cwd" title={activeWorkspace}>{activeWorkspace}</small>}
          </div>
          <div className="topbar-actions">
            <button
              type="button"
              className="new-conversation"
              onClick={newConversation}
              disabled={running}
              title="Nouvelle conversation"
            >
              <MessageSquarePlus aria-hidden="true" />
              <span>Nouvelle conversation</span>
            </button>
            <div className="model-badge">
              <span />
              {selectedModel || activeAgent?.model || "modèle par défaut"}
            </div>
          </div>
        </header>

        <div className="conversation">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="orbit"><span>A</span></div>
              <p className="eyebrow">Précision avant vitesse</p>
              <h2>Que veux-tu explorer&nbsp;?</h2>
              <p className="welcome-copy">
                Le superviseur peut utiliser les skills disponibles, appeler ses modules
                et déléguer des tâches à ses agents enfants.
              </p>
              <div className="suggestions">
                {starterPrompts.map((starter) => (
                  <button key={starter} onClick={() => setPrompt(starter)}>
                    <span>{starter}</span>
                    <b>↗</b>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="message-list" aria-live="polite">
              {messages.map((message) => (
                <Fragment key={message.id}>
                  {message.role === "assistant" && message.runId &&
                    traceEventsForRun(traceEvents, message.runId).length > 0 && (
                      <ProcessTrace
                        events={traceEventsForRun(traceEvents, message.runId)}
                        live={running && activeRunId === message.runId}
                      />
                    )}
                  <article className={`message ${message.role} ${message.error ? "error" : ""}`}>
                    <div className="message-avatar">{message.role === "user" ? "X" : "A"}</div>
                    <div>
                      <div className="message-meta">
                        <strong>{message.role === "user" ? "Toi" : activeAgent?.id || "Agent"}</strong>
                        <span>{message.meta}</span>
                      </div>
                      {message.role === "assistant" ? (
                        <>
                          <MarkdownMessage content={message.content} />
                          <MessageArtifacts
                            sessionId={activeSessionId}
                            artifacts={message.artifacts}
                          />
                        </>
                      ) : (
                        <p>{message.content}</p>
                      )}
                    </div>
                  </article>
                </Fragment>
              ))}
              {running && activeRunId &&
                !messages.some((message) => message.role === "assistant" && message.runId === activeRunId) &&
                traceEventsForRun(traceEvents, activeRunId).length > 0 && (
                <ProcessTrace
                  events={traceEventsForRun(traceEvents, activeRunId)}
                  live
                />
              )}
              {running && (
                <article className="message assistant thinking">
                  <div className="message-avatar">A</div>
                  <div><div className="pulse"><i /><i /><i /></div></div>
                </article>
              )}
            </div>
          )}
        </div>

        <ApprovalPanel
          approvals={approvals}
          running={running}
          progress={approvalProgress}
          onResolve={(approval, approved) => void resolveApproval(approval, approved)}
          onResolveBatch={(approved) => void resolveApprovalBatch(approved)}
        />

        {currentPlan && (
          <CurrentPlanPanel
            plan={currentPlan}
            expanded={planExpanded}
            running={running}
            onExpandedChange={setPlanExpanded}
            onDelete={() => void deleteCurrentPlan()}
            onStepChange={(step, completed) => void updatePlanStep(step, completed)}
          />
        )}

        <form className="composer" onSubmit={submit}>
          <input
            ref={imageInput}
            className="composer-file-input"
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            onChange={(event) => {
              void addImageFiles(Array.from(event.target.files || []));
              event.target.value = "";
            }}
          />
          {selectedSkills.length > 0 && (
            <div className="selected-skills">
              {selectedSkills.map((skill) => (
                <button type="button" key={skill} onClick={() => toggleSkill(skill)}>
                  {skill}<span>×</span>
                </button>
              ))}
            </div>
          )}
          {composerImages.length > 0 && (
            <div className="image-previews">
              {composerImages.map((image) => (
                <figure key={image.id}>
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={image.dataUrl} alt={image.name} />
                  <figcaption>{image.name}</figcaption>
                  <button
                    type="button"
                    aria-label={`Retirer ${image.name}`}
                    onClick={() => setComposerImages((current) => current.filter((item) => item.id !== image.id))}
                  >×</button>
                </figure>
              ))}
            </div>
          )}
          {commandMatches.length > 0 && (
            <div className="slash-autocomplete" role="listbox" aria-label="Commandes RPPL">
              {commandMatches.map((command, index) => (
                <button
                  type="button"
                  role="option"
                  aria-selected={index === commandSelection}
                  className={index === commandSelection ? "selected" : ""}
                  key={command.command}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => chooseCommand(command)}
                >
                  <strong>{command.command}</strong>
                  <span>{command.description}</span>
                  <small>{command.skill}</small>
                </button>
              ))}
            </div>
          )}
          <textarea
            ref={textarea}
            value={prompt}
            onChange={(event) => {
              setPrompt(event.target.value);
              setCommandSelection(0);
            }}
            onPaste={(event) => {
              const images = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
              if (images.length) {
                event.preventDefault();
                void addImageFiles(images);
              }
            }}
            onKeyDown={(event) => {
              if (commandMatches.length) {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setCommandSelection((value) => (value + 1) % commandMatches.length);
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setCommandSelection((value) => (
                    value - 1 + commandMatches.length
                  ) % commandMatches.length);
                  return;
                }
                if (event.key === "Tab" || event.key === "Enter") {
                  event.preventDefault();
                  chooseCommand(commandMatches[commandSelection] || commandMatches[0]);
                  return;
                }
              }
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            placeholder="Décris une tâche, une question ou une incertitude…"
            rows={2}
            aria-label="Message au kernel"
          />
          <ContextMeter status={contextStatus} />
          {attachmentError && <small className="attachment-error">{attachmentError}</small>}
          <ComposerControls
            providers={catalog?.providers || []}
            providerId={providerId}
            model={selectedModel}
            securityMode={securityMode}
            reasoning={reasoning}
            running={running}
            stopRequested={stopRequested}
            canAttach={!running && composerImages.length < 4}
            canSend={Boolean(prompt.trim()) && !running}
            vision={Boolean(activeProvider?.vision)}
            onAttach={() => imageInput.current?.click()}
            onSecurityModeChange={(mode) => void changeSecurityMode(mode)}
            onModelChange={(nextProvider, model) => {
              selectProvider(nextProvider);
              setSelectedModel(model);
            }}
            onReasoningChange={setReasoning}
            onStop={() => void stopRun()}
          />
        </form>
      </section>
    </main>
  );
}
