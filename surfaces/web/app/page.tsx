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
  Bot,
  CalendarClock,
  ChevronDown,
  ChevronRight,
  FolderOpen,
  GitBranch,
  MessageSquarePlus,
  PlugZap,
  Sparkles,
  Trash2,
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
type PlanStep = {
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
type CurrentPlan = {
  plan_id: string;
  title: string;
  steps: PlanStep[];
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

type Approval = {
  approval_id: string;
  session_id: string;
  tool_name: string;
  path: string | null;
  reason: string;
  justification: string;
  risks: string[];
  run_id?: string;
  created_at?: string;
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

type ContextStatus = {
  provider_id: string;
  model: string | null;
  context_window_tokens: number | null;
  estimated_history_tokens: number;
  estimated_ratio: number | null;
  compaction_threshold_ratio: number;
  compaction_count: number;
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
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  last_retryable: boolean;
  in_flight: boolean;
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
  if (editor.frequency_kind === "minutes") return `Toutes les ${editor.frequency_interval} minute(s)`;
  if (editor.frequency_kind === "hours") return `Toutes les ${editor.frequency_interval} heure(s)`;
  if (editor.frequency_kind === "weekly") return `Chaque ${days[editor.frequency_weekday]} à ${editor.frequency_time}`;
  if (editor.frequency_kind === "yearly") {
    return `Tous les ans, le ${editor.frequency_monthday} ${months[editor.frequency_month - 1]} à ${editor.frequency_time}`;
  }
  return `Tous les jours à ${editor.frequency_time}`;
}

function compactTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value);
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
  trigger?: "user" | "resume" | "cron" | "cron_resume" | "cron_test";
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
  const [securityMode, setSecurityMode] = useState<"safe" | "limited" | "power">("limited");
  const [providerId, setProviderId] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [reasoning, setReasoning] = useState<"minimal" | "low" | "medium" | "high" | "xhigh">("medium");
  const [composerImages, setComposerImages] = useState<ComposerImage[]>([]);
  const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
  const [attachmentError, setAttachmentError] = useState("");
  const [stopRequested, setStopRequested] = useState(false);
  const [managementModal, setManagementModal] = useState<"projects" | "agents" | "skills" | "providers" | "crons" | null>(null);
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
  const [cronEditor, setCronEditor] = useState<CronEditor | null>(null);
  const [cronTestApprovals, setCronTestApprovals] = useState<Approval[]>([]);
  const [cronTestMessage, setCronTestMessage] = useState("");
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
    const response = await fetch("/api/kernel/crons");
    if (!response.ok) throw new Error("Impossible de charger les cronjobs");
    setCronJobs(await response.json());
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

  function createCron() {
    setCronEditor(cronEditorFromJob({
      id: "", name: "Nouvelle routine", schedule: "0 9 * * *", prompt: "",
      workspace: activeWorkspace, agent_id: agentId, skills: selectedSkills,
      security_mode: securityMode, provider_id: providerId || null,
      model: selectedModel || null, reasoning, enabled: false, auto_resume: true,
      session_id: "", next_run_at: null, last_run_at: null, last_status: null,
      last_error: null, in_flight: false,
      last_retryable: false,
    }, true));
    setCronTestApprovals([]);
    setCronTestMessage("");
  }

  async function saveCron() {
    if (!cronEditor) return;
    setSavingResource(true);
    setManagementError("");
    const body = {
      name: cronEditor.name, schedule: scheduleFromEditor(cronEditor), prompt: cronEditor.prompt,
      workspace: cronEditor.workspace, agent_id: cronEditor.agent_id,
      skills: cronEditor.skills, security_mode: cronEditor.security_mode,
      provider_id: cronEditor.provider_id, model: cronEditor.model,
      reasoning: cronEditor.reasoning, enabled: cronEditor.enabled,
      auto_resume: cronEditor.auto_resume,
    };
    try {
      const response = await fetch(
        `/api/kernel/crons${cronEditor.creating ? "" : `/${cronEditor.id}`}`,
        {
          method: cronEditor.creating ? "POST" : "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Enregistrement impossible");
      await refreshCrons();
      if (cronEditor.creating) {
        setCronEditor(cronEditorFromJob(data as CronJob));
        setCronTestMessage("Routine enregistrée et inactive. Teste-la avant de l’activer.");
      } else {
        setCronEditor(null);
      }
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Enregistrement impossible");
    } finally {
      setSavingResource(false);
    }
  }

  async function deleteCron(id: string) {
    if (!window.confirm("Supprimer ce cronjob ? Son historique de session sera conservé.")) return;
    const response = await fetch(`/api/kernel/crons/${id}`, { method: "DELETE" });
    if (!response.ok) {
      const data = await response.json();
      setManagementError(data.detail || "Suppression impossible");
      return;
    }
    await refreshCrons();
  }

  async function runCronNow(id: string) {
    const response = await fetch(`/api/kernel/crons/${id}/run`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      setManagementError(data.detail || "Lancement impossible");
      return;
    }
    await refreshCrons();
  }

  async function toggleCron(job: CronJob, enabled: boolean) {
    const body = {
      name: job.name, schedule: job.schedule, prompt: job.prompt,
      workspace: job.workspace, agent_id: job.agent_id, skills: job.skills,
      security_mode: job.security_mode, provider_id: job.provider_id,
      model: job.model, reasoning: job.reasoning, enabled,
      auto_resume: job.auto_resume,
    };
    const response = await fetch(`/api/kernel/crons/${job.id}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      setManagementError(data.detail || "Modification impossible");
      return;
    }
    await refreshCrons();
  }

  async function refreshCronTestApprovals(sessionId: string) {
    const response = await fetch("/api/kernel/approvals");
    if (!response.ok) return [];
    const pending: Approval[] = await response.json();
    const routineApprovals = pending.filter((item) => item.session_id === sessionId);
    setCronTestApprovals(routineApprovals);
    return routineApprovals;
  }

  async function testCron(id: string) {
    setSavingResource(true);
    setCronTestMessage("Test en cours…");
    setCronTestApprovals([]);
    try {
      const response = await fetch(`/api/kernel/crons/${id}/test`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Test impossible");
      if (data.status === "approval_pending") {
        const pending = await refreshCronTestApprovals(data.session_id);
        setCronTestMessage(
          `${pending.length} autorisation(s) à valider pour les prochaines exécutions.`,
        );
      } else if (data.status === "completed") {
        setCronTestMessage("Test réussi. La routine est prête.");
      } else {
        setCronTestMessage(data.errors?.map((error: {message: string}) => error.message).join("\n")
          || `Test terminé avec le statut ${data.status}.`);
      }
      await refreshCrons();
    } catch (error) {
      setCronTestMessage(error instanceof Error ? error.message : "Test impossible");
    } finally {
      setSavingResource(false);
    }
  }

  async function resolveCronTestApprovals(approved: boolean) {
    if (!cronEditor || cronTestApprovals.length === 0) return;
    const current = cronTestApprovals;
    setSavingResource(true);
    setCronTestApprovals([]);
    setCronTestMessage(approved ? "Autorisations enregistrées · test en reprise…" : "Refus enregistrés · test en reprise…");
    try {
      const response = await fetch("/api/kernel/approvals/resolve-batch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          approval_ids: current.map((approval) => approval.approval_id),
          approved,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Résolution impossible");
      if (data.status === "approval_pending") {
        const pending = await refreshCronTestApprovals(cronEditor.session_id);
        setCronTestMessage(`${pending.length} nouvelle(s) autorisation(s) à examiner.`);
      } else if (data.status === "completed") {
        setCronTestMessage(
          approved
            ? "Test réussi. Ces autorisations exactes seront réutilisées par la routine."
            : "Test terminé après refus.",
        );
      } else {
        setCronTestMessage(data.errors?.map((error: {message: string}) => error.message).join("\n")
          || `Test terminé avec le statut ${data.status}.`);
      }
      await refreshCrons();
    } catch (error) {
      setCronTestApprovals(current);
      setCronTestMessage(error instanceof Error ? error.message : "Résolution impossible");
    } finally {
      setSavingResource(false);
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
    setApprovals([]);
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
          <nav className="context-nav" aria-label="Contexte du run">
            <button className="context-card" onClick={() => setManagementModal("projects")}>
              <span className="context-icon" aria-hidden="true"><FolderOpen /></span>
              <span>
                <small>Projet actif</small>
                <strong>{activeWorkspaceInfo?.name || "Choisir un projet"}</strong>
                <em>{activeWorkspaceInfo?.path || "Aucun CWD"}</em>
              </span>
              <ChevronRight className="context-chevron" aria-hidden="true" />
            </button>
            <button className="context-card" onClick={() => setManagementModal("agents")}>
              <span className="context-icon agent-icon" aria-hidden="true"><Bot /></span>
              <span>
                <small>Agent actif</small>
                <strong>{activeAgent?.id || "Aucun agent"}</strong>
                <em>{activeAgent?.provider || "Non configuré"}</em>
              </span>
              <ChevronRight className="context-chevron" aria-hidden="true" />
            </button>
            <button className="context-card" onClick={() => setManagementModal("skills")}>
              <span className="context-icon" aria-hidden="true"><Sparkles /></span>
              <span>
                <small>Skills</small>
                <strong>{selectedSkills.length
                  ? `${selectedSkills.length} sélectionnée${selectedSkills.length > 1 ? "s" : ""}`
                  : "Aucune sélection"}</strong>
                <em>{catalog?.skills.length || 0} disponible{(catalog?.skills.length || 0) > 1 ? "s" : ""}</em>
              </span>
              <ChevronRight className="context-chevron" aria-hidden="true" />
            </button>
            <button className="context-card" onClick={() => setManagementModal("providers")}>
              <span className="context-icon" aria-hidden="true"><PlugZap /></span>
              <span>
                <small>Providers</small>
                <strong>{catalog?.providers.length || 0} configuré{(catalog?.providers.length || 0) > 1 ? "s" : ""}</strong>
                <em>Défaut · {catalog?.default_provider || "aucun"}</em>
              </span>
              <ChevronRight className="context-chevron" aria-hidden="true" />
            </button>
            <button className="context-card" onClick={() => setManagementModal("crons")}>
              <span className="context-icon" aria-hidden="true"><CalendarClock /></span>
              <span>
                <small>Automatisations</small>
                <strong>{cronJobs.filter((job) => job.enabled).length} active{cronJobs.filter((job) => job.enabled).length > 1 ? "s" : ""}</strong>
                <em>Cronjobs et reprises</em>
              </span>
              <ChevronRight className="context-chevron" aria-hidden="true" />
            </button>
          </nav>

          <section className="rail-section history-section">
          <div className="section-title">
            <p className="eyebrow">Historique</p>
            <span>{sessions.length}</span>
          </div>
          <div className="session-list">
            {sessions.map((session) => (
              <div
                key={session.session_id}
                className={[
                  "session-entry",
                  session.session_id === activeSessionId ? "active" : "",
                  unreadSessionIds.has(session.session_id) ? "unread" : "",
                ].filter(Boolean).join(" ")}
              >
                <button
                  className="session-open"
                  onClick={() => void openSession(session.session_id)}
                  title={session.prompt}
                >
                  <span className={`session-state ${session.status}`} />
                  <span>
                    <strong>
                      {session.prompt || "Session sans titre"}
                      {session.trigger?.startsWith("cron") && (
                        <em className="automation-chip">Routine</em>
                      )}
                    </strong>
                    <small>{new Date(session.updated_at).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" })}</small>
                  </span>
                </button>
                {unreadSessionIds.has(session.session_id) && (
                  <span className="session-unread" title="Nouveau résultat" aria-label="Nouveau résultat" />
                )}
                <button
                  className="session-resume"
                  hidden={!["failed", "timeout", "partial", "cancelled"].includes(session.status)}
                  onClick={() => void resumeSession(session.session_id)}
                  disabled={running}
                  aria-label="Reprendre la session"
                  title="Reprendre à partir des traces persistées"
                >↻</button>
                <button
                  className="session-delete"
                  onClick={() => void deleteSession(session.session_id)}
                  aria-label="Supprimer la session"
                  title="Supprimer la session"
                >
                  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path d="M5 7h14M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5" />
                  </svg>
                </button>
              </div>
            ))}
            {sessions.length === 0 && <p className="empty-label">Aucune session</p>}
          </div>
          </section>
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
                  setCronEditor(null);
                }} aria-label="Fermer">×</button>
              </div>
            </header>

            {managementError && <div className="management-error">{managementError}</div>}

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
                <div className="resource-fields">
                  <label className="field-wide">
                    Nom
                    <input value={cronEditor.name} onChange={(event) =>
                      setCronEditor((current) => current && ({ ...current, name: event.target.value }))}
                    />
                  </label>
                  <label>
                    Répétition
                    <select value={cronEditor.frequency_kind} onChange={(event) =>
                      setCronEditor((current) => current && ({
                        ...current, frequency_kind: event.target.value as CronFrequencyKind,
                      }))}>
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
                        onChange={(event) => setCronEditor((current) => current && ({
                          ...current, frequency_interval: Math.max(1, Number(event.target.value)),
                        }))}
                      />
                    </label>
                  )}
                  {cronEditor.frequency_kind === "weekly" && (
                    <label>
                      Jour
                      <select value={cronEditor.frequency_weekday} onChange={(event) =>
                        setCronEditor((current) => current && ({
                          ...current, frequency_weekday: Number(event.target.value),
                        }))}>
                        {["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"]
                          .map((day, index) => <option value={index} key={day}>{day}</option>)}
                      </select>
                    </label>
                  )}
                  {cronEditor.frequency_kind === "yearly" && (
                    <>
                      <label>
                        Mois
                        <select value={cronEditor.frequency_month} onChange={(event) =>
                          setCronEditor((current) => current && ({
                            ...current, frequency_month: Number(event.target.value),
                          }))}>
                          {["Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
                            "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
                            .map((month, index) => <option value={index + 1} key={month}>{month}</option>)}
                        </select>
                      </label>
                      <label>
                        Jour du mois
                        <input type="number" min="1" max="31" value={cronEditor.frequency_monthday}
                          onChange={(event) => setCronEditor((current) => current && ({
                            ...current, frequency_monthday: Math.min(31, Math.max(1, Number(event.target.value))),
                          }))}
                        />
                      </label>
                    </>
                  )}
                  {["daily", "weekly", "yearly"].includes(cronEditor.frequency_kind) && (
                    <label>
                      Heure
                      <input type="time" value={cronEditor.frequency_time} onChange={(event) =>
                        setCronEditor((current) => current && ({
                          ...current, frequency_time: event.target.value,
                        }))}
                      />
                    </label>
                  )}
                  <div className="field-wide cron-summary">
                    <span>Prochaine règle</span>
                    <strong>{describeCron(cronEditor)}</strong>
                  </div>
                  <label>
                    Agent
                    <select value={cronEditor.agent_id} onChange={(event) =>
                      setCronEditor((current) => current && ({
                        ...current, agent_id: event.target.value, security_mode: securityMode,
                      }))}>
                      {catalog?.agents.map((agent) => (
                        <option value={agent.id} key={agent.id}>{agent.id}</option>
                      ))}
                    </select>
                  </label>
                  <div className="permission-inherited">
                    <span>Permissions héritées de l’agent actif</span>
                    <strong>{cronEditor.security_mode}</strong>
                  </div>
                  <label className="field-wide">
                    Workspace
                    <input value={cronEditor.workspace} onChange={(event) =>
                      setCronEditor((current) => current && ({ ...current, workspace: event.target.value }))}
                    />
                  </label>
                  <label className="field-wide">
                    Demande exécutée
                    <textarea value={cronEditor.prompt} onChange={(event) =>
                      setCronEditor((current) => current && ({ ...current, prompt: event.target.value }))}
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
                </div>
                <p className="cron-note">
                  Teste la routine avant de l’activer. Les demandes ASK validées pendant le test
                  seront mémorisées pour cette routine, uniquement pour l’action et la cible exactes.
                </p>
                {!cronEditor.creating && (
                  <div className="cron-test-panel">
                    <div>
                      <strong>Validation avant automatisation</strong>
                      <small>{cronTestMessage || "Lance un test pour détecter les autorisations nécessaires."}</small>
                    </div>
                    {cronTestApprovals.length > 0 && (
                      <ul>
                        {cronTestApprovals.map((approval) => (
                          <li key={approval.approval_id}>
                            <strong>{approval.tool_name}</strong>
                            <span>{approval.justification}</span>
                            {approval.path && <code>{approval.path}</code>}
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="cron-test-actions">
                      {cronTestApprovals.length > 0 ? (
                        <>
                          <button disabled={savingResource}
                            onClick={() => void resolveCronTestApprovals(false)}>Tout refuser</button>
                          <button className="primary" disabled={savingResource}
                            onClick={() => void resolveCronTestApprovals(true)}>Tout autoriser durablement</button>
                        </>
                      ) : (
                        <button disabled={savingResource || cronEditor.in_flight}
                          onClick={() => void testCron(cronEditor.id)}>
                          {savingResource ? "Test en cours…" : "Tester la routine"}
                        </button>
                      )}
                    </div>
                  </div>
                )}
                <div className="resource-editor-actions">
                  <button onClick={() => setCronEditor(null)}>Annuler</button>
                  <button className="primary" onClick={() => void saveCron()}
                    disabled={savingResource || !cronEditor.name.trim() || !cronEditor.prompt.trim()}>
                    {savingResource ? "Enregistrement…" : "Enregistrer"}
                  </button>
                </div>
              </div>
            )}

            {managementModal === "crons" && !cronEditor && (
              <div className="management-body management-list">
                {cronJobs.map((job) => (
                  <div className={`cron-row ${job.enabled ? "" : "disabled"}`} key={job.id}>
                    <button className="cron-main"
                      onClick={() => {
                        setCronEditor(cronEditorFromJob(job));
                        setCronTestApprovals([]);
                        setCronTestMessage("");
                      }}>
                      <span className={`row-status ${job.in_flight ? "running" : job.last_status || ""}`} />
                      <span>
                        <strong>{job.name}</strong>
                        <small>{describeCron(cronEditorFromJob(job))} · {job.agent_id} · {job.workspace}</small>
                        <em>
                          {job.in_flight
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
                      <button onClick={() => setCronEditor(cronEditorFromJob(job))}
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
                  <button disabled={running} onClick={() => void resolveApprovalBatch(false)}>
                    Tout refuser
                  </button>
                  <button className="approve" disabled={running} onClick={() => void resolveApprovalBatch(true)}>
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
                  <button disabled={running} onClick={() => void resolveApproval(approval, false)}>
                    Refuser
                  </button>
                  <button
                    className="approve"
                    disabled={running}
                    onClick={() => void resolveApproval(approval, true)}
                  >
                    Autoriser
                  </button>
                </div>
              </article>
            ))}
          </section>
        )}

        {approvalProgress && (
          <div className="approval-progress" role="status" aria-live="polite">
            <span className="approval-spinner" />
            <strong>{approvalProgress}</strong>
          </div>
        )}

        {currentPlan && (
          <section className={`current-plan ${planExpanded ? "expanded" : ""}`}>
            <header>
              <div>
                <p className="eyebrow">Plan courant</p>
                <strong>
                  {currentPlan.steps.filter((step) => step.status === "completed").length}
                  {" "}tâches sur {currentPlan.steps.length} terminées
                </strong>
                <small>{currentPlan.title}</small>
              </div>
              <div className="plan-actions">
                <button
                  type="button"
                  onClick={() => void deleteCurrentPlan()}
                  aria-label="Supprimer le plan"
                  title="Supprimer le plan"
                ><Trash2 size={19} /></button>
                <button
                  type="button"
                  onClick={() => setPlanExpanded((value) => !value)}
                  aria-label={planExpanded ? "Replier le plan" : "Ouvrir le plan"}
                ><ChevronDown size={22} /></button>
              </div>
            </header>
            {planExpanded && (
              <div className="plan-steps">
                {currentPlan.steps.map((step) => (
                  <label className={`plan-step ${step.status}`} key={step.id}>
                    <input
                      type="checkbox"
                      checked={step.status === "completed"}
                      disabled={running}
                      onChange={(event) => void updatePlanStep(step, event.target.checked)}
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
          <div
            className={[
              "context-meter",
              contextStatus?.estimated_ratio == null ? "unknown" : "",
              (contextStatus?.estimated_ratio || 0) >= 0.7 ? "critical"
                : (contextStatus?.estimated_ratio || 0) >= 0.5 ? "warning" : "",
            ].filter(Boolean).join(" ")}
            title={
              contextStatus?.context_window_tokens
                ? `${contextStatus.estimated_history_tokens.toLocaleString("fr-FR")} tokens estimés sur ${contextStatus.context_window_tokens.toLocaleString("fr-FR")}`
                : "Fenêtre de contexte inconnue — utilise /model-context"
            }
          >
            <div className="context-meter-label">
              <span>Contexte</span>
              {contextStatus?.estimated_ratio != null && contextStatus.context_window_tokens ? (
                <strong>
                  {Math.min(100, Math.round(contextStatus.estimated_ratio * 100))} %
                  <small>
                    {compactTokens(contextStatus.estimated_history_tokens)}
                    {" / "}
                    {compactTokens(contextStatus.context_window_tokens)}
                  </small>
                </strong>
              ) : (
                <strong>Fenêtre inconnue <small>/model-context</small></strong>
              )}
            </div>
            <div className="context-meter-track" aria-hidden="true">
              <span
                className="context-meter-fill"
                style={{
                  width: contextStatus?.estimated_ratio == null
                    ? "100%"
                    : `${Math.min(100, contextStatus.estimated_ratio * 100)}%`,
                }}
              />
              <i className="context-threshold" title="Compaction automatique à 70 %" />
            </div>
          </div>
          {attachmentError && <small className="attachment-error">{attachmentError}</small>}
          <div className="composer-footer">
            <div className="composer-controls">
              <button
                type="button"
                className="composer-icon-button"
                onClick={() => imageInput.current?.click()}
                disabled={running || composerImages.length >= 4}
                aria-label="Ajouter une image"
                title={
                  activeProvider?.vision
                    ? "Ajouter une image"
                    : "Ajouter une image · analyse locale Gemma 4"
                }
              >+</button>
              <label title="Niveau de permission">
                <span className={`permission-dot ${securityMode}`} />
                <select
                  value={securityMode}
                  onChange={(event) => void changeSecurityMode(event.target.value as typeof securityMode)}
                  aria-label="Niveau de permission"
                >
                  <option value="safe">safe</option>
                  <option value="limited">limited</option>
                  <option value="power">power</option>
                </select>
              </label>
              <label className="model-control" title="Provider et modèle">
                <select
                  value={`${providerId}::${selectedModel}`}
                  disabled={running}
                  onChange={(event) => {
                    const [nextProvider, ...modelParts] = event.target.value.split("::");
                    selectProvider(nextProvider);
                    setSelectedModel(modelParts.join("::"));
                  }}
                  aria-label="Provider et modèle"
                >
                  {catalog?.providers.map((provider) => (
                    <optgroup key={provider.id} label={provider.id}>
                      {provider.models.map((model) => (
                        <option key={`${provider.id}:${model}`} value={`${provider.id}::${model}`}>
                          {model}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </label>
              <label title="Niveau de raisonnement">
                <select
                  value={reasoning}
                  disabled={running}
                  onChange={(event) => setReasoning(event.target.value as typeof reasoning)}
                  aria-label="Niveau de raisonnement"
                >
                  <option value="minimal">minimal</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="xhigh">xhigh</option>
                </select>
              </label>
            </div>
            <div className="composer-run-actions">
              {running && (
                <button
                  type="button"
                  className="stop"
                  onClick={() => void stopRun()}
                  disabled={stopRequested}
                  aria-label="Arrêter le run"
                  title="Arrêter le run"
                ><span /></button>
              )}
              <button className="send" disabled={!prompt.trim() || running} aria-label="Envoyer">
                ↑
              </button>
            </div>
          </div>
        </form>
      </section>
    </main>
  );
}
