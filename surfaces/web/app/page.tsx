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
import { playRunChime, primeRunChime } from "./run-chime";
import {
  ApprovalPanel,
  type Approval,
} from "./components/approval-panel";
import {
  ComposerControls,
  type KnowledgeMode,
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
import { RoutineEditor } from "./components/routine-editor";
import { ProviderEditor } from "./components/provider-editor";
import { KanbanBoard } from "./components/kanban-board";
import { ResourceEditorForm } from "./components/resource-editor-form";
import {
  GitChangeCard,
  GitCommitDialog,
  GitReviewBody,
  GitToolbar,
  type GitFile,
  type GitSnapshot,
} from "./components/git-workspace";
import {
  ChromeEmpty,
  Dock,
  IconRail,
  Resizer,
  Shell,
  SidePanel,
  SidePanelSection,
  type DockTab,
  type RailEntry,
} from "./components/layout/shell";
import { usePanels } from "./components/layout/use-panels";
import { FileExplorer } from "./components/layout/file-explorer";
import { ThemePicker } from "./components/layout/theme-picker";
import { AgentAvatar } from "./components/agent-avatar";
import { CompactionIndicator } from "./components/compaction-indicator";
import { ModuleSettingsPanel } from "./components/module-settings-panel";
import { Icon } from "./theme/theme-context";
import {
  fileExplorerKernelUrl,
  formatApiDetail,
  readApiPayload,
} from "./lib/api";
import { normalizeResourceId } from "./lib/format";
import { conversationReducer } from "./lib/conversation";
import {
  DockTabId,
  SlashCommand,
  routineClearCommand,
  Catalog,
  ConfigurableModule,
  Message,
  Workspace,
  ComposerImage,
  toImageMediaType,
  ImagePreview,
  ManagedResource,
  TelegramAgentConfig,
  ResourceEditor,
  ManagedProvider,
  codexAuthHelpUrl,
  codexApiUrl,
  KnowledgePage,
  ContentHome,
  ComposerPreferences,
  composerPreferencesKey,
  parseMarkdownResource,
  buildMarkdownResource,
  resourceList,
  RunArtifact,
  SessionSummary,
  gitSnapshotForRun,
} from "./lib/model";
import { runStatsByRunId, traceEventsForRun, type TraceEvent } from "./lib/trace";
import { cronEditorFromJob, describeCron } from "./lib/cron";
import { ProcessTrace } from "./components/process-trace";
import {
  MarkdownMessage,
  MessageArtifacts,
  RunCost,
  UserMessageImages,
} from "./components/message-parts";
import { useGitReview } from "./components/use-git-review";
import { useRoutines } from "./components/use-routines";

const starterPrompts = [
  "Cartographie les risques et les inconnues de ce projet.",
  "Propose trois sous-tâches indépendantes et délègue-les.",
  "Résume les décisions récentes avec leurs conséquences.",
];

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
  const [soundEnabled, setSoundEnabled] = useState(true);
  const approvalsRef = useRef(0);
  const [clearingSession, setClearingSession] = useState(false);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState("");
  const [workspaceInput, setWorkspaceInput] = useState("");
  // Rien n'entre dans le contexte par défaut : `off` est délibéré, la
  // connaissance ne s'invite pas d'elle-même.
  const [knowledgeMode, setKnowledgeMode] = useState<KnowledgeMode>("off");
  const [knowledgePages, setKnowledgePages] = useState<KnowledgePage[]>([]);
  const [knowledgeSelection, setKnowledgeSelection] = useState<string[]>([]);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [knowledgeError, setKnowledgeError] = useState("");
  // Emplacement du dossier de données. Le kernel l'ouvre au démarrage : un
  // changement ne prend effet qu'au suivant, et l'interface doit le dire.
  const [contentHome, setContentHome] = useState<ContentHome | null>(null);
  const [contentHomeInput, setContentHomeInput] = useState("");
  const [contentHomeBusy, setContentHomeBusy] = useState(false);
  const [contentHomeNotice, setContentHomeNotice] = useState("");
  const [contentHomeError, setContentHomeError] = useState("");
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
  const [imagePreview, setImagePreview] = useState<ImagePreview | null>(null);
  const [managedResources, setManagedResources] = useState<ManagedResource[]>([]);
  const [resourceEditor, setResourceEditor] = useState<ResourceEditor | null>(null);
  const [managementError, setManagementError] = useState("");
  const [savingResource, setSavingResource] = useState(false);
  const [repairingHierarchy, setRepairingHierarchy] = useState(false);
  const [providerModels, setProviderModels] = useState<Record<string, string[]>>({});
  const [modelsLoading, setModelsLoading] = useState(false);
  const [managedProviders, setManagedProviders] = useState<ManagedProvider[]>([]);
  const [defaultProvider, setDefaultProvider] = useState("");
  const [providerEditor, setProviderEditor] = useState<(ManagedProvider & { creating: boolean }) | null>(null);
  const [moduleSettings, setModuleSettings] = useState<ConfigurableModule[]>([]);
  const [codexConnected, setCodexConnected] = useState(false);
  const [codexAuthLoading, setCodexAuthLoading] = useState(false);
  const [composerPreferencesReady, setComposerPreferencesReady] = useState(false);
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>([]);
  const [commandSelection, setCommandSelection] = useState(0);
  const [currentPlan, setCurrentPlan] = useState<CurrentPlan | null>(null);
  const [planExpanded, setPlanExpanded] = useState(true);
  // Etat du shell : panneaux redimensionnables + onglet actif du dock droit.
  const panels = usePanels();
  const [dockTab, setDockTab] = useState<DockTabId>("git");
  const [explorerFile, setExplorerFile] = useState<{ path: string; content: string } | null>(null);
  const [explorerError, setExplorerError] = useState("");
  // Incrémenté après un téléversement : l'URL d'avatar est stable, seul ce
  // paramètre force le navigateur à recharger l'image.
  const [avatarVersion, setAvatarVersion] = useState(0);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const avatarInput = useRef<HTMLInputElement>(null);
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

  // Le son est joué depuis le `finally` d'un envoi : la fermeture y capture
  // l'état du début du run, pas celui de sa fin. Une référence donne le compte
  // au moment où il est lu.
  useEffect(() => {
    approvalsRef.current = approvals.length;
  }, [approvals]);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(composerPreferencesKey);
      if (raw) {
        const stored = JSON.parse(raw) as Partial<ComposerPreferences>;
        if (["safe", "limited", "power"].includes(String(stored.securityMode))) {
          setSecurityMode(stored.securityMode as ComposerPreferences["securityMode"]);
        }
        if (typeof stored.providerId === "string") setProviderId(stored.providerId);
        if (["minimal", "low", "medium", "high", "xhigh"].includes(String(stored.reasoning))) {
          setReasoning(stored.reasoning as ComposerPreferences["reasoning"]);
        }
        if (typeof stored.soundEnabled === "boolean") setSoundEnabled(stored.soundEnabled);
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
      securityMode, providerId, reasoning, soundEnabled,
    };
    restoredComposerPreferences.current = preferences;
    window.localStorage.setItem(composerPreferencesKey, JSON.stringify(preferences));
  }, [composerPreferencesReady, securityMode, providerId, reasoning, soundEnabled]);

  /**
   * Le catalogue porte les agents, skills, providers, tools et modules : sans
   * lui, tous les écrans de configuration sont vides. Un échec doit donc être
   * visible, et le code de statut conservé — un 500 ne se corrige pas comme un
   * kernel éteint.
   */
  const loadCatalog = useCallback(async () => {
    // Deux pannes distinctes, deux gestes distincts : un kernel éteint se
    // lance, un kernel mal configuré se corrige. Les confondre derrière un même
    // message obligeait à deviner laquelle des deux on avait.
    let response: Response;
    try {
      response = await fetch("/api/kernel/catalog");
    } catch {
      setCatalogError("Kernel injoignable. Lance-le avec `agents start`.");
      return null;
    }
    if (!response.ok) {
      // Le corps porte la cause réelle — fichier manquant, chemin cherché. La
      // jeter pour n'afficher qu'un code laissait devant un « 503 » sans le
      // moindre moyen d'agir.
      const detail = await response
        .json()
        .then((body: { detail?: unknown }) => formatApiDetail(body.detail))
        .catch(() => "");
      setCatalogError(detail || `Le kernel a répondu ${response.status} sur /api/catalog.`);
      return null;
    }
    try {
      const data = (await response.json()) as Catalog;
      setCatalog(data);
      setCatalogError("");
      return data;
    } catch {
      setCatalogError("Réponse du kernel illisible sur /api/catalog.");
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const attempt = async () => {
      const data = await loadCatalog();
      if (cancelled) return;
      if (!data) {
        // Le kernel démarre souvent après la surface. Sans cette reprise, la
        // page restait définitivement vide : rien ne relançait la lecture.
        timer = setTimeout(() => void attempt(), 5000);
        return;
      }
      // `agentId` vaut « main » avant toute lecture : on ne le conserve que si
      // le catalogue le confirme, sinon on retombe sur le premier agent réel.
      setAgentId((current) =>
        data.agents.some((agent) => agent.id === current)
          ? current
          : data.agents[0]?.id || current,
      );
    };
    void attempt();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loadCatalog]);

  useEffect(() => {
    const stored = window.localStorage.getItem("amk.workspaces");
    let recent: Workspace[] = [];
    try {
      recent = stored ? JSON.parse(stored) : [];
    } catch {
      window.localStorage.removeItem("amk.workspaces");
    }
    Promise.all([
      fetch("/api/kernel/workspaces/current").then(async (response) => {
        if (!response.ok) throw new Error("Workspace du kernel indisponible");
        return response.json() as Promise<Workspace>;
      }),
      fetch("/api/kernel/workspaces/recent")
        .then((response) => response.ok ? response.json() as Promise<Workspace[]> : [])
        .catch(() => [] as Workspace[]),
    ])
      .then(([current, recovered]) => {
        // The kernel CWD is only the first-run default. Once the user has made
        // a project list, do not silently add it again on every launch.
        // `localStorage` is scoped by browser origin: localhost and 127.0.0.1
        // have different lists. Sessions are durable on the API side, so merge
        // their project roots to recover navigation after an origin change.
        const merged = new Map<string, Workspace>();
        [...recent, ...recovered].forEach((workspace) => merged.set(workspace.path, workspace));
        const known = merged.size > 0 ? [...merged.values()] : [current];
        // « Aucun projet » est le point de départ : on ouvre sur ce que l'agent
        // a fait, pas sur le dernier dossier où l'on codait. Un projet mémorisé
        // est restitué, la chaîne vide comprise — elle signifie « aucun ».
        const remembered = window.localStorage.getItem("amk.activeWorkspace");
        const active = remembered !== null && (remembered === "" || known.some(
          (item) => item.path === remembered,
        ))
          ? remembered
          : "";
        setWorkspaces(known);
        window.localStorage.setItem("amk.workspaces", JSON.stringify(known));
        // La liste des projets arrive après le premier chargement des sessions,
        // donc parfois après l'atterrissage sur le canal. Restaurer le projet
        // mémorisé à ce moment-là écrasait l'alignement que l'ouverture de
        // session venait de faire : l'en-tête montrait le canal, la modale
        // affichait l'ancien projet. La session ouverte a le dernier mot.
        if (activeSessionIdRef.current) return;
        setActiveWorkspace(active);
        window.localStorage.setItem("amk.activeWorkspace", active);
      })
      .catch((error) => setWorkspaceError(error instanceof Error ? error.message : "Erreur workspace"));
  }, []);

  const activeAgent = useMemo(
    () => catalog?.agents.find((agent) => agent.id === agentId),
    [catalog, agentId],
  );
  // Le kernel additionne les skills de l'agent et celles de la requête
  // (`agent_factory`), puis dédoublonne. L'affichage suit la même règle : ce que
  // l'agent apporte est montré sans croix — cela se change dans sa
  // configuration, pas ici — et seul l'ajout ponctuel est retirable.
  const agentSkills = useMemo(() => activeAgent?.skills ?? [], [activeAgent]);
  const extraSkills = useMemo(
    () => selectedSkills.filter((skill) => !agentSkills.includes(skill)),
    [selectedSkills, agentSkills],
  );
  const activeSkillCount = agentSkills.length + extraSkills.length;

  // Le kernel encadre la compaction de deux événements. On remonte le fil : le
  // premier des deux rencontré tranche, sans avoir à apparier les paires ni à
  // filtrer par run — une compaction porte sur la session entière.
  const compacting = useMemo(() => {
    if (!running) return false;
    for (let index = traceEvents.length - 1; index >= 0; index -= 1) {
      const { type } = traceEvents[index];
      if (type === "context.compacted" || type === "context.compaction_noop") return false;
      if (type === "context.pre_compaction_snapshot") return true;
    }
    return false;
  }, [running, traceEvents]);

  const activeProvider = useMemo(
    () => catalog?.providers.find((provider) => provider.id === providerId),
    [catalog, providerId],
  );
  const activeWorkspaceInfo = useMemo(
    () => workspaces.find((workspace) => workspace.path === activeWorkspace),
    [workspaces, activeWorkspace],
  );
  const activeSession = useMemo(
    () => sessions.find((session) => session.session_id === activeSessionId),
    [activeSessionId, sessions],
  );
  // Le canal de l'agent : son fil permanent, où convergent routines et
  // Telegram. Il n'appartient à aucun projet, d'où l'absence de workspace.
  const isAgentChannel = activeSession?.trigger === "agent_channel";
  const conversationWorkspace = activeSessionId
    ? activeSession?.effective_workspace || activeSession?.workspace
    : activeWorkspace;
  const conversationWorkspaceInfo = useMemo(
    () => workspaces.find((workspace) => workspace.path === conversationWorkspace),
    [workspaces, conversationWorkspace],
  );

  // L'historique est déjà filtré sur le projet à la source : `refreshSessions`
  // passe `workspace`, et la projection ne conserve en plus que ce qui
  // n'appartient à aucun projet — routines, espace personnel de l'agent et
  // canaux Telegram. Refiltrer ici masquerait précisément ces trois-là.

  const {
    gitSnapshot,
    gitBranches,
    gitSelectedFile,
    gitReviewSnapshot,
    gitBusy,
    gitError,
    gitCommitMessage,
    gitCommitSource,
    setGitSelectedFile,
    setGitReviewSnapshot,
    setGitCommitMessage,
    setGitError,
    switchGitBranch,
    proposeGitCommit,
    commitGitChanges,
  } = useGitReview({
    workspace: conversationWorkspace,
    isAgentChannel,
    running,
    providerId,
    selectedModel,
  });

  const routines = useRoutines({
    managementModal,
    agentId,
    selectedSkills,
    securityMode,
    providerId,
    selectedModel,
    reasoning,
    notificationSessionId: sessions.find(
      (item) => item.trigger === "agent_channel" && item.agent_id === agentId,
    )?.session_id || "",
    workflowCreatorAvailable: Boolean(
      catalog?.skills.some((skill) => skill.name === "workflow-creator"),
    ),
    savingResource,
    activeSessionIdRef,
    setManagementError,
    setUnreadSessionIds,
    setSavingResource,
  });

  const {
    cronJobs,
    cronEditor,
    testingCron,
    cronWorkflowMutationBusy,
    unreadCronCount,
    closeCronEditor,
    createCron,
    openCronEditor,
    saveCron,
    deleteCron,
    runCronNow,
    toggleCron,
  } = routines;



  const runStats = useMemo(() => runStatsByRunId(traceEvents), [traceEvents]);

  const commandMatches = useMemo(() => {
    const value = prompt.trimStart();
    if (!value.startsWith("/") || value.includes(" ")) return [];
    const query = value.toLowerCase();
    // `/clear` vaut pour toute conversation ouverte, pas seulement la boîte de
    // routines. Il n'a en revanche aucun sens sur une session pas encore créée.
    const availableCommands = activeSessionId
      ? [routineClearCommand, ...slashCommands]
      : slashCommands;
    return availableCommands.filter((item) => item.command.startsWith(query));
  }, [prompt, slashCommands, activeSessionId]);

  useEffect(() => {
    // Les commandes globales — celles des skills de `content-agents` — existent
    // sans projet. Abandonner faute de workspace vidait l'autocomplétion dans
    // toute la vue Accueil : plus de `/secu`, plus de `/plan`, plus rien. L'API
    // accepte l'absence de workspace et retombe sur la racine.
    const requete = activeWorkspace
      ? `?workspace=${encodeURIComponent(activeWorkspace)}`
      : "";
    fetch(`/api/kernel/commands${requete}`)
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
    // Rien à faire tant qu'on reste sur le même agent : un modèle choisi à la
    // main doit tenir jusqu'à ce qu'on en change.
    if (
      composerPreferencesApplied.current
      && lastComposerAgent.current === activeAgent.id
    ) return;

    // Le provider peut venir des préférences; le modèle vient toujours de
    // l'agent. Persister le modèle revenait à masquer son défaut pour de bon.
    const restored = restoredComposerPreferences.current;
    const provider =
      (!composerPreferencesApplied.current && restored
        ? catalog.providers.find((item) => item.id === restored.providerId)
        : undefined)
      || catalog.providers.find((item) => item.id === activeAgent.provider)
      || catalog.providers[0];
    if (!provider) return;
    setProviderId(provider.id);
    setSelectedModel(
      // Le modèle de l'agent ne vaut que s'il appartient au provider retenu.
      provider.models.includes(activeAgent.model || "")
        ? activeAgent.model || provider.model || provider.models[0] || ""
        : provider.model || provider.models[0] || "",
    );
    // Même règle que le modèle : le niveau vient de l'agent, le composer peut
    // le changer pour un message. Sans cela, `power` gardé d'un agent à l'autre
    // s'appliquerait à un agent réglé plus prudemment, sans qu'on l'ait voulu.
    if (lastComposerAgent.current !== activeAgent.id) {
      setSecurityMode(activeAgent.security_mode || "limited");
    }
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
    // Sans projet actif, la vue est celle de l'agent hors projet : son canal
    // permanent et son espace personnel. C'est un état à part entière, pas une
    // absence de filtre — sans `agent_id`, le kernel renverrait l'historique de
    // tous les projets à la fois.
    const requete = workspace
      ? `workspace=${encodeURIComponent(workspace)}`
      : `agent_id=${encodeURIComponent(agentId)}`;
    const response = await fetch(`/api/kernel/sessions?${requete}&include_channels=true`);
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
          // Pendant un run, le SSE est la source de vérité de la conversation
          // ouverte. La rouvrir ici toutes les 2,5 s remettait activeRunId à
          // null : les événements arrivaient bien, mais leur trace disparaissait
          // aussitôt derrière « connexion au journal… ».
          && runningSessionId.current !== activeSessionIdRef.current
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

  // Au démarrage, atterrir dans le canal de l'agent plutôt que dans le dernier
  // projet ouvert. Le projet est un contexte de travail qu'on choisit ; le
  // canal est l'endroit où l'agent parle — routines, Telegram, échanges hors
  // projet. C'est là qu'on veut voir ce qui s'est passé pendant l'absence.
  const canalOuvertAuDemarrage = useRef(false);
  useEffect(() => {
    if (canalOuvertAuDemarrage.current || activeSessionId || !sessions.length) return;
    const canal = sessions.find(
      (item) => item.trigger === "agent_channel" && item.agent_id === agentId,
    );
    if (!canal) return;
    canalOuvertAuDemarrage.current = true;
    void openSession(canal.session_id);
    // `openSession` est stable pour ce besoin : la référence garantit un seul
    // atterrissage, quelles que soient les recompositions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions, activeSessionId, agentId]);

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
      if (event.key === "Escape") closeManagement();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  // closeManagement deliberately reads the current editor/busy state.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [managementModal]);

  useEffect(() => {
    if (!imagePreview) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setImagePreview(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [imagePreview]);

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
    if (managementModal !== "settings") return;
    setManagementError("");
    setContentHomeNotice("");
    setContentHomeError("");
    void loadContentHome();
    fetch("/api/kernel/admin/module-settings")
      .then(async (response) => {
        if (!response.ok) throw new Error("Impossible de charger les paramètres");
        return response.json();
      })
      .then((data: ConfigurableModule[]) => setModuleSettings(data))
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
    // `loadCatalog` remonte l'échec dans `catalogError`. L'ancienne version
    // ignorait une réponse non-ok sans rien dire, et l'écran se vidait sans
    // qu'aucun message n'explique pourquoi.
    await loadCatalog();
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
          user_memory: false,
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
      telegram: kind === "agents" ? {
        enabled: false,
        hide_session: false,
        user_id: "",
        bot_token: "",
      } : undefined,
    });
    setManagementError("");
  }

  async function repairAgentHierarchy() {
    setRepairingHierarchy(true);
    setManagementError("");
    try {
      const response = await fetch("/api/admin/agents/repair-hierarchy", { method: "POST" });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        setManagementError(detail?.detail || `Correction impossible (${response.status})`);
        return;
      }
      await refreshCatalog();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Correction impossible");
    } finally {
      setRepairingHierarchy(false);
    }
  }

  async function saveResource() {
    if (!resourceEditor) return;
    const normalizedId = resourceEditor.creating
      ? normalizeResourceId(resourceEditor.id)
      : resourceEditor.id;
    if (!normalizedId) {
      setManagementError("L’identifiant doit contenir au moins une lettre ou un chiffre.");
      return;
    }
    const editor = normalizedId === resourceEditor.id ? resourceEditor : {
      ...resourceEditor,
      id: normalizedId,
      frontmatter: {
        ...resourceEditor.frontmatter,
        [resourceEditor.kind === "agents" ? "id" : "name"]: normalizedId,
      },
    };
    if (normalizedId !== resourceEditor.id) setResourceEditor(editor);
    if (editor.kind === "agents" && editor.telegram?.enabled) {
      const telegram = editor.telegram;
      const hasUser = Boolean(telegram.user_id.trim() || telegram.user_id_configured);
      const hasToken = Boolean(telegram.bot_token.trim() || telegram.bot_token_configured);
      if (!hasUser || !hasToken) {
        setManagementError(
          "Telegram nécessite l’identifiant numérique de l’utilisateur et le token du bot.",
        );
        return;
      }
      if (telegram.user_id.trim() && !/^[1-9]\d*$/.test(telegram.user_id.trim())) {
        setManagementError("L’identifiant utilisateur Telegram doit être un entier positif.");
        return;
      }
      if (telegram.bot_token.trim() && !/^\d+:[A-Za-z0-9_-]{20,}$/.test(telegram.bot_token.trim())) {
        setManagementError("Le token du bot Telegram n’a pas le format fourni par BotFather.");
        return;
      }
    }
    setSavingResource(true);
    setManagementError("");
    const endpoint = `/api/kernel/admin/${editor.kind}${editor.creating ? "" : `/${editor.id}`}`;
    try {
      const response = await fetch(endpoint, {
        method: editor.creating ? "POST" : "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          id: editor.id,
          content: buildMarkdownResource(editor),
          telegram: editor.kind === "agents" ? editor.telegram : undefined,
        }),
      });
      // `readApiPayload` survit à une réponse non-JSON. Une erreur 500 renvoie
      // « Internal Server Error » en texte brut, et `response.json()` échouait
      // alors sur « Unexpected token 'I' » — un message qui parle du parseur au
      // lieu de dire que le serveur a refusé.
      await readApiPayload(response);
      const refreshed = await fetch(`/api/kernel/admin/${editor.kind}`);
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

  function updateResourceId(value: string, normalize = false) {
    setResourceEditor((current) => {
      if (!current || !current.creating) return current;
      const id = normalize ? normalizeResourceId(value) : value;
      return {
        ...current,
        id,
        frontmatter: {
          ...current.frontmatter,
          [current.kind === "agents" ? "id" : "name"]: id,
        },
      };
    });
  }

  function updateTelegramField(
    name: keyof TelegramAgentConfig,
    value: string | boolean,
  ) {
    setResourceEditor((current) => current && current.kind === "agents" ? ({
      ...current,
      telegram: {
        enabled: false,
        hide_session: false,
        user_id: "",
        bot_token: "",
        ...current.telegram,
        [name]: value,
      },
    }) : current);
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

  /**
   * Ajoute ou retire plusieurs valeurs en une écriture. Enchaîner
   * `toggleEditorListField` sur les six outils d'un module ferait six mises à
   * jour successives, chacune lisant l'état d'avant : seule la dernière
   * survivrait.
   */
  function setEditorListValues(
    name: string,
    values: string[],
    next: boolean,
    defaults: string[] = [],
  ) {
    const hasExplicitValue = resourceEditor
      ? Object.prototype.hasOwnProperty.call(resourceEditor.frontmatter, name)
      : false;
    const current = hasExplicitValue ? resourceList(resourceEditor?.frontmatter[name]) : defaults;
    const changed = new Set(values);
    updateEditorField(
      name,
      next
        ? [...current, ...values.filter((value) => !current.includes(value))]
        : current.filter((value) => !changed.has(value)),
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
      telegram: kind === "agents" ? resource.telegram : undefined,
    });
    setManagementError("");
  }

  function agentHasAvatar(agentId: string): boolean {
    return Boolean(managedResources.find((item) => item.id === agentId)?.has_avatar);
  }

  async function refreshAgentAvatars() {
    // Recharge la liste pour que `has_avatar` reflète l'état réel, et pousse la
    // version afin que l'image servie à URL constante soit redemandée.
    try {
      const response = await fetch("/api/kernel/admin/agents");
      setManagedResources(await readApiPayload<ManagedResource[]>(response));
    } catch {
      /* la liste sera rafraîchie au prochain chargement de la modale */
    }
    setAvatarVersion((value) => value + 1);
    await refreshCatalog();
  }

  async function uploadAgentAvatar(agentId: string, file: File) {
    setAvatarBusy(true);
    setManagementError("");
    try {
      const buffer = await file.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      let binary = "";
      // Par tranches : `String.fromCharCode(...bytes)` dépasse la limite
      // d'arguments sur une image de quelques centaines de kilooctets.
      for (let index = 0; index < bytes.length; index += 8192) {
        binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
      }
      const response = await fetch(
        `/api/kernel/admin/agents/${encodeURIComponent(agentId)}/avatar`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ data_base64: btoa(binary) }),
        },
      );
      await readApiPayload(response);
      await refreshAgentAvatars();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Envoi impossible");
    } finally {
      setAvatarBusy(false);
    }
  }

  async function removeAgentAvatar(agentId: string) {
    setAvatarBusy(true);
    setManagementError("");
    try {
      const response = await fetch(
        `/api/kernel/admin/agents/${encodeURIComponent(agentId)}/avatar`,
        { method: "DELETE" },
      );
      await readApiPayload(response);
      await refreshAgentAvatars();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Suppression impossible");
    } finally {
      setAvatarBusy(false);
    }
  }

  async function purgeSessions() {
    // Le périmètre est celui de la vue courante, et le libellé le dit : effacer
    // « tout » au sens de la base emporterait des conversations d'autres projets
    // que l'utilisateur n'a pas sous les yeux.
    const perimetre = activeWorkspace
      ? `du projet ${activeWorkspace.split(/[\\/]/).pop()}`
      : `hors projet de ${agentId}`;
    if (!window.confirm(
      `Supprimer définitivement les conversations ${perimetre} ?\n`
      + "Les canaux d'agent et un run en cours sont conservés.",
    )) return;
    const requete = activeWorkspace
      ? `workspace=${encodeURIComponent(activeWorkspace)}`
      : `agent_id=${encodeURIComponent(agentId)}`;
    try {
      const response = await fetch(`/api/kernel/sessions/purge?${requete}`, { method: "POST" });
      await readApiPayload(response);
      newConversation();
      await refreshSessions();
    } catch (error) {
      setAttachmentError(error instanceof Error ? error.message : "Suppression impossible");
    }
  }

  async function deleteResource(kind: "agents" | "skills", id: string) {
    if (!window.confirm(`Supprimer ${id} ?`)) return;
    setManagementError("");
    const response = await fetch(`/api/kernel/admin/${kind}/${id}`, { method: "DELETE" });
    try {
      await readApiPayload(response);
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Suppression impossible");
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
      if (kind === "llama-cpp") {
        return {
          ...current,
          kind,
          connection_type: "local",
          base_url: null,
          port: current.port || 8123,
          n_gpu_layers: current.n_gpu_layers ?? 999,
          num_ctx: current.num_ctx || 16384,
          flash_attn: current.flash_attn ?? true,
          preserve_thinking: current.preserve_thinking ?? false,
          startup_timeout_seconds: current.startup_timeout_seconds || 240,
          llama_args: current.llama_args || [],
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

  function updateModuleSetting(moduleId: string, fieldName: string, value: unknown) {
    setModuleSettings((current) => current.map((module) => module.id !== moduleId
      ? module
      : {
          ...module,
          fields: module.fields.map((field) => field.name === fieldName
            ? { ...field, value }
            : field),
        }));
  }

  async function saveModuleSettings(module: ConfigurableModule) {
    setSavingResource(true);
    setManagementError("");
    try {
      const values = Object.fromEntries(module.fields.map((field) => [
        field.name,
        field.type === "secret" ? (field.value || "") : field.value,
      ]));
      const response = await fetch(
        `/api/kernel/admin/module-settings/${encodeURIComponent(module.id)}`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ values }),
        },
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Enregistrement impossible");
      setModuleSettings((current) => current.map((item) => item.id === module.id ? data : item));
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

  async function clearRoutineSession() {
    if (!activeSessionId || clearingSession) return;
    if (!window.confirm(
      isAgentChannel
        ? "Vider définitivement tous les messages et toutes les traces du canal de cet agent ?"
        : "Vider définitivement cette conversation ? Messages, traces, plan et"
          + " pièces jointes seront supprimés, sans possibilité de retour.",
    )) return;
    setClearingSession(true);
    setAttachmentError("");
    try {
      const response = await fetch(
        `/api/kernel/sessions/${encodeURIComponent(activeSessionId)}/clear`,
        { method: "POST" },
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Impossible de vider la session");
      setMessages([]);
      setTraceEvents([]);
      setApprovals([]);
      setActiveRunId(null);
      setCurrentPlan(null);
      setPrompt("");
      await refreshSessions();
      await refreshContextStatus();
    } catch (error) {
      setAttachmentError(
        error instanceof Error ? error.message : "Impossible de vider la session",
      );
    } finally {
      setClearingSession(false);
    }
  }

  // Une demande d'autorisation n'est pas une fin : le run est suspendu et
  // n'ira pas plus loin sans toi. Deux timbres pour deux réactions.
  function playEndOfRunChime() {
    if (!soundEnabled) return;
    playRunChime(approvalsRef.current > 0 ? "attention" : "done");
  }

  async function resumeSession(sessionId: string) {
    setManagementError("");
    primeRunChime();
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
      playEndOfRunChime();
      setRunning(false);
      runningSessionId.current = null;
    }
  }

  function openManagement(section: ResourceSection) {
    setManagementError("");
    setResourceEditor(null);
    setProviderEditor(null);
    closeCronEditor();
    setManagementModal(section);
  }

  function closeManagement() {
    if (cronEditor && (cronWorkflowMutationBusy || savingResource || testingCron)) return;
    setManagementModal(null);
    setResourceEditor(null);
    setProviderEditor(null);
    closeCronEditor();
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
    const nextApprovals = latestRunId
      ? sessionPending.filter((item) => item.run_id === latestRunId)
      : sessionPending;
    // `setApprovals` ne devient visible qu'au rendu React suivant. Le carillon
    // est pourtant joué dans le `finally` du run courant : garder la référence
    // synchrone évite qu'un ASK tout juste reçu soit annoncé comme une fin.
    approvalsRef.current = nextApprovals.length;
    setApprovals(nextApprovals);
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
    // Le projet suit la session qu'on ouvre. Sans cet alignement, l'en-tête
    // annonçait « espace personnel » pendant que la modale affichait `test8`
    // comme actif et que l'historique listait les sessions de ce projet : trois
    // affirmations contradictoires pour un seul état.
    const projetDeLaSession = session.workspace || "";
    if (projetDeLaSession !== activeWorkspace) {
      setActiveWorkspace(projetDeLaSession);
      window.localStorage.setItem("amk.activeWorkspace", projetDeLaSession);
    }
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
          images: message.role === "user"
            ? message.artifacts
              ?.filter((artifact) => artifact.kind === "input_image")
              .map((artifact) => ({
                id: artifact.artifact_id,
                name: artifact.name,
                mediaType: toImageMediaType(artifact.media_type),
                dataUrl: `/api/kernel/artifacts/${encodeURIComponent(sessionId)}/${encodeURIComponent(artifact.artifact_id)}`,
              }))
            : undefined,
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

  function startEventStream(sessionId: string, afterSequence?: number) {
    const cursor = Math.max(
      0,
      afterSequence
        ?? traceEvents.reduce((latest, item) => Math.max(latest, item.sequence || 0), 0),
    );
    const source = new EventSource(
      `/api/kernel/sessions/${sessionId}/events?after_sequence=${cursor}`,
    );
    source.addEventListener("trace", (raw) => {
      const event = JSON.parse((raw as MessageEvent).data) as TraceEvent;
      if (event.type === "session.started" && !event.parent_run_id) {
        setActiveRunId(event.run_id);
      } else if (
        event.parent_run_id && event.type === "agent.started"
      ) {
        // Si la connexion s'établit après `session.started`, la première
        // délégation porte encore explicitement l'identité du run racine.
        setActiveRunId((current) => current || event.parent_run_id);
      } else if (
        event.type.startsWith("context.")
        || event.type === "run.transitioned"
        || event.type === "session.completed"
      ) {
        // Les événements de contexte sont journalisés sur le run racine, même
        // lorsqu'ils décrivent le travail d'un délégué. Ils permettent de
        // récupérer après une reconnexion qui aurait manqué le début du run.
        setActiveRunId((current) => current || event.run_id);
      }
      if (event.type.startsWith("context.")) {
        // La projection backend est mise à jour dans la même écriture que
        // l'événement. Rafraîchir immédiatement rend visibles les compactages
        // et évite d'attendre le prochain tick de cinq secondes.
        void refreshContextStatus();
      }
      setTraceEvents((current) => {
        const key = event.sequence
          ? `sequence:${event.sequence}`
          : `${event.timestamp}:${event.type}:${event.run_id}`;
        return current.some((item) => (
          item.sequence
            ? `sequence:${item.sequence}`
            : `${item.timestamp}:${item.type}:${item.run_id}`
        ) === key)
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
    // Une session reste rattachée au projet où elle a commencé : son historique,
    // ses diffs et ses chemins s'y réfèrent. La déplacer la rendrait incohérente.
    // Choisir un projet ouvre donc une conversation neuve.
    //
    // La comparaison porte sur le projet de la *conversation*, pas sur celui
    // qui était sélectionné. Un canal permanent écrit dans l'espace personnel de
    // son agent quel que soit le projet affiché : rouvrir le projet déjà
    // sélectionné laissait ce canal ouvert et le geste restait sans effet. On
    // crée un projet, on clique dessus, et le message suivant part quand même
    // ailleurs — un jeu entier s'est écrit dans `workspaces/main` de cette
    // façon, pendant que le rail annonçait `test9`.
    if (conversationWorkspace !== path && activeSessionId && !running) newConversation();
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
    try {
      const response = await fetch("/api/kernel/workspaces/validate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ path }),
      });
      // `response.json()` brut transformait un 502 HTML en erreur JSON illisible.
      const data = await readApiPayload<Workspace>(response);
      registerWorkspace(data);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Dossier invalide");
    }
  }

  async function openKnowledge() {
    setKnowledgeError("");
    setKnowledgeOpen(true);
    try {
      const response = await fetch(
        `/api/kernel/admin/knowledge?agent_id=${encodeURIComponent(agentId)}`,
      );
      const data = await readApiPayload<{ pages: KnowledgePage[] }>(response);
      setKnowledgePages(data.pages);
      // Une page supprimée depuis la dernière sélection ne doit pas rester
      // cochée : elle serait ignorée à l'envoi sans que rien ne le dise.
      const disponibles = new Set(data.pages.map((page) => page.slug));
      setKnowledgeSelection((current) => current.filter((slug) => disponibles.has(slug)));
    } catch (error) {
      setKnowledgeError(error instanceof Error ? error.message : "Bibliothèque illisible");
      setKnowledgePages([]);
    }
  }

  async function loadContentHome() {
    try {
      const response = await fetch("/api/kernel/admin/content-home");
      const data = await readApiPayload<ContentHome>(response);
      setContentHome(data);
      setContentHomeInput(data.configured || "");
    } catch {
      /* l'écran reste utilisable sans cette information */
    }
  }

  async function saveContentHome(path: string) {
    setContentHomeBusy(true);
    setContentHomeError("");
    setContentHomeNotice("");
    try {
      const response = await fetch("/api/kernel/admin/content-home", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: path.trim() || null }),
      });
      await readApiPayload(response);
      await loadContentHome();
      setContentHomeNotice(
        path.trim()
          ? "Emplacement enregistré. Il sera utilisé au prochain démarrage d’AMK."
          : "Emplacement par défaut rétabli. Effectif au prochain démarrage d’AMK.",
      );
    } catch (error) {
      setContentHomeError(
        error instanceof Error ? error.message : "Enregistrement impossible",
      );
    } finally {
      setContentHomeBusy(false);
    }
  }

  async function pickContentHome() {
    setContentHomeError("");
    try {
      const response = await fetch("/api/kernel/workspaces/pick", { method: "POST" });
      const data = await readApiPayload<{ path: string }>(response);
      if (data.path) setContentHomeInput(data.path);
    } catch (error) {
      setContentHomeError(
        error instanceof Error ? error.message : "Sélection impossible",
      );
    }
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
    if (!content || running || clearingSession) return;
    if (content.toLowerCase() === "/clear") {
      await clearRoutineSession();
      return;
    }
    const submittedImages = composerImages;
    const secretCommand = content.match(/^\/secret\s+(\S+)\s+[\s\S]+$/i);
    const displayedContent = secretCommand
      ? `/secret ${secretCommand[1]} ••••••••`
      : content;
    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: displayedContent,
      images: submittedImages,
      meta: `${agentId}${activeSkillCount ? ` · ${activeSkillCount} skill${activeSkillCount > 1 ? "s" : ""}` : ""}`,
    };
    const sessionId = activeSessionId || crypto.randomUUID();
    const continuingSession = Boolean(activeSessionId);
    // Une longue conversation peut contenir plusieurs milliers d'événements.
    // Les événements chargés avec la session portent leur séquence : le flux
    // live doit repartir juste après, sans rejouer tout le journal avant de
    // montrer le nouveau run.
    const eventCursor = continuingSession
      ? traceEvents.reduce((latest, item) => Math.max(latest, item.sequence || 0), 0)
      : 0;
    runningSessionId.current = sessionId;
    setStopRequested(false);
    setActiveSessionId(sessionId);
    if (!continuingSession) setTraceEvents([]);
    setActiveRunId(null);
    setMessages((current) => continuingSession ? [...current, userMessage] : [userMessage]);
    setPrompt("");
    setComposerImages([]);
    setAttachmentError("");
    approvalsRef.current = 0;
    primeRunChime();
    setRunning(true);
    const eventSource = startEventStream(sessionId, eventCursor);
    try {
      const response = await fetch("/api/kernel/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          prompt: content,
          agent_id: agentId,
          skills: selectedSkills,
          workspace: isAgentChannel
            ? undefined
            : conversationWorkspace || activeWorkspace || undefined,
          session_id: sessionId,
          security_mode: securityMode,
          knowledge_mode: knowledgeMode,
          knowledge_pages: knowledgeMode === "manual" ? knowledgeSelection : [],
          provider_id: providerId || undefined,
          model: selectedModel || undefined,
          reasoning,
          images: submittedImages.map((image) => ({
            name: image.name,
            media_type: image.mediaType,
            data_base64: image.dataUrl.split(",", 2)[1],
          })),
        }),
      });
      // `response.json()` brut masquait les refus du kernel derrière des
      // erreurs JSON illisibles quand la réponse n'était pas du JSON.
      const data = await readApiPayload<{
        status: string;
        output: string | null;
        errors?: { message: string }[];
        session_id: string;
        run_id: string;
        artifacts?: RunArtifact[];
      }>(response);
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
      playEndOfRunChime();
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
    primeRunChime();
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
      playEndOfRunChime();
      setRunning(false);
      runningSessionId.current = null;
    }
  }

  async function resolveApprovalBatch(approved: boolean) {
    if (approvals.length === 0) return;
    const previousApprovals = approvals;
    primeRunChime();
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
      playEndOfRunChime();
      setRunning(false);
      runningSessionId.current = null;
    }
  }

  function openGitReview(snapshot: GitSnapshot, file?: GitFile) {
    setGitReviewSnapshot(snapshot);
    setGitSelectedFile(file || snapshot.files[0] || null);
    setDockTab("git");
    panels.setOpen("right", true);
  }

  /**
   * Le rail ne s'affiche que lorsque le panneau est replié — il en est le
   * substitut, pas un doublon. Sa première entrée rouvre donc le panneau ;
   * les suivantes ouvrent les modales de configuration.
   */
  // Les canaux permanents ne s'affichent plus dans une vue projet : leur
  // destination est « Accueil ». La pastille porte donc seule le signal qu'un
  // résultat de routine vient d'y tomber pendant qu'on travaille ailleurs.
  // `unreadSessionIds` est alimenté par le sondage des routines, qui ignore le
  // projet courant : l'information est là même quand la session n'est pas
  // affichée.
  //
  // Le compte porte sur les non-lus *absents de la vue courante*. Ceux du
  // projet affiché portent déjà leur point dans la liste; ce qui reste vient du
  // sondage des routines, dont la session de notification est le canal permanent
  // de l'agent instanciateur. Depuis « Accueil », ces sessions sont affichées,
  // donc le compte retombe à zéro — ce qui est juste : on y est.
  const canauxNonLus = useMemo(
    () =>
      [...unreadSessionIds].filter(
        (identifiant) => !sessions.some((session) => session.session_id === identifiant),
      ).length,
    [sessions, unreadSessionIds],
  );

  const railEntries: RailEntry[] = [
    {
      id: "panel",
      icon: "panelLeftOpen",
      label: "Ouvrir l'espace de travail",
      badge: unreadSessionIds.size || undefined,
      onSelect: () => panels.setOpen("left", true),
    },
    {
      id: "home",
      icon: "agent",
      label: "Accueil — canaux permanents",
      badge: canauxNonLus || undefined,
      onSelect: () => chooseWorkspace(""),
    },
    { id: "projects", icon: "project", label: "Projets", onSelect: () => openManagement("projects") },
    { id: "kanban", icon: "plan", label: "Kanban", onSelect: () => openManagement("kanban") },
    { id: "agents", icon: "agent", label: "Agents", onSelect: () => openManagement("agents") },
    { id: "skills", icon: "skill", label: "Skills", onSelect: () => openManagement("skills") },
    { id: "providers", icon: "provider", label: "Providers", onSelect: () => openManagement("providers") },
    { id: "settings", icon: "settings", label: "Paramètres", onSelect: () => openManagement("settings") },
    {
      id: "crons",
      icon: "automation",
      label: "Automatisations",
      badge: unreadCronCount || undefined,
      onSelect: () => openManagement("crons"),
    },
  ];

  const dockTabs: DockTab[] = [
    { id: "git", icon: "git", label: "Révision Git", badge: gitSnapshot?.files.length || undefined },
    { id: "files", icon: "fileTree", label: "Fichiers du projet" },
  ];

  // Statut git par chemin : colore l'explorateur sans second appel réseau.
  const changedPaths = new Map<string, string>(
    (gitSnapshot?.files ?? []).map((file) => [file.path, file.status.toLowerCase()] as const),
  );

  const openExplorerFile = async (path: string) => {
    if (!conversationWorkspace) return;
    setExplorerError("");
    try {
      const query = new URLSearchParams({ workspace: conversationWorkspace, path });
      const response = await fetch(`/api/kernel/files/content?${query}`);
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${response.status}`);
      }
      setExplorerFile((await response.json()) as { path: string; content: string });
    } catch (error) {
      setExplorerFile(null);
      setExplorerError(error instanceof Error ? error.message : "Lecture impossible.");
    }
  };

  return (
    <Shell
      leftOpen={panels.left.open}
      rightOpen={panels.right.open}
      leftWidth={panels.left.width}
      rightWidth={panels.right.width}
      resizing={panels.resizing}
    >
      <IconRail
        brand="A"
        onBrandClick={newConversation}
        collapsed={panels.left.open}
        entries={railEntries}
        footer={
          <>
            <ThemePicker />
            {/* Même règle que dans le panneau déplié : un kernel qui fonctionne
                n'a rien à signaler, seule la panne mérite d'être vue. */}
            {catalogError && (
              <span
                className="rail-item rail-status"
                data-state="offline"
                data-tip={catalogError}
                role="status"
                aria-label="État du kernel"
              >
                <Icon name="activity" size="md" />
              </span>
            )}
          </>
        }
      />

      <SidePanel
        title="Espace de travail"
        collapsed={!panels.left.open}
        footer={
          <>
            {/* L'état nominal du kernel n'apprenait rien : il est connecté la
                quasi-totalité du temps, et son provider est déjà lisible dans le
                composer. Seule la panne mérite d'occuper cette place. */}
            {catalogError && (
              <div className="kernel-status offline" role="status">
                <span />
                <div>
                  <strong>Kernel hors ligne</strong>
                  <small>{catalogError}</small>
                </div>
              </div>
            )}
            <button
              type="button"
              className="panel-settings"
              onClick={() => openManagement("settings")}
            >
              <Icon name="settings" size="sm" />
              <span>Paramètres</span>
            </button>
            <ThemePicker placement="panel" />
          </>
        }
        actions={
          <>
            <button
              type="button"
              className="ibtn sm"
              data-tip="Nouvelle conversation"
              data-tip-side="bottom-end"
              aria-label="Nouvelle conversation"
              disabled={running}
              onClick={newConversation}
            >
              <Icon name="newChat" size="sm" />
            </button>
            <button
              type="button"
              className="ibtn sm"
              data-tip="Replier le panneau"
              data-tip-side="bottom-end"
              aria-label="Replier le panneau latéral"
              onClick={() => panels.toggle("left")}
            >
              <Icon name="panelLeftClose" size="sm" />
            </button>
          </>
        }
        resizer={
          <Resizer
            side="end"
            label="Largeur du panneau latéral"
            active={panels.resizing === "left"}
            onPointerDown={panels.startResize("left")}
            onKeyDown={panels.nudge("left")}
          />
        }
      >
        <div ref={railContent} className="side-panel-stack">
          <SidePanelSection>
            <ResourceNavigation
              onHome={() => chooseWorkspace("")}
              homeActive={!activeWorkspace}
              homeUnread={canauxNonLus}
              projectName={activeWorkspaceInfo?.name}
              workspaceCount={workspaces.length}
              agentId={activeAgent?.id}
              agentCount={catalog?.agents.length || 0}
              activeSkillCount={activeSkillCount}
              availableSkillCount={catalog?.skills.length || 0}
              providerCount={catalog?.providers.length || 0}
              defaultProvider={catalog?.default_provider}
              settingsCount={catalog?.configurable_modules?.length || 0}
              cronCount={cronJobs.length}
              activeCronCount={cronJobs.filter((job) => job.enabled).length}
              unreadCronCount={unreadCronCount}
              onOpen={openManagement}
            />
          </SidePanelSection>

          {/* Les skills actives ne sont pas répétées ici : le composer les affiche
              déjà, à l'endroit où elles s'appliquent. */}

          {/* L'historique occupe tout l'espace restant et scrolle seul : le
              contexte reste visible quelle que soit la longueur de la liste. */}
          <SidePanelSection
            title="Historique"
            count={sessions.length}
            grow
            action={sessions.length > 1 && (
              <button
                type="button"
                className="section-action"
                onClick={() => void purgeSessions()}
                disabled={running}
                aria-label="Supprimer les conversations de cette vue"
                title="Supprimer les conversations de cette vue"
              ><Icon name="remove" size="sm" /></button>
            )}
          >
            <SessionHistory
              sessions={sessions}
              knownAgents={(catalog?.agents || []).map((agent) => agent.id)}
              activeSessionId={activeSessionId}
              unreadSessionIds={unreadSessionIds}
              running={running}
              onOpen={(sessionId) => void openSession(sessionId)}
              onResume={(sessionId) => void resumeSession(sessionId)}
              onDelete={(sessionId) => void deleteSession(sessionId)}
            />
          </SidePanelSection>
        </div>
      </SidePanel>

      {imagePreview && (
        <div className="image-lightbox-backdrop" onMouseDown={() => setImagePreview(null)}>
          <section
            className="image-lightbox"
            role="dialog"
            aria-modal="true"
            aria-labelledby="image-preview-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header>
              <h2 id="image-preview-title">{imagePreview.name}</h2>
              <button
                type="button"
                aria-label="Fermer l’image"
                onClick={() => setImagePreview(null)}
                autoFocus
              ><Icon name="close" size="sm" /></button>
            </header>
            {/* Preview sources are runtime artifact URLs or user-provided data URLs. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={imagePreview.source} alt={imagePreview.name} />
          </section>
        </div>
      )}

      {managementModal && (
        <div className="management-backdrop" onMouseDown={closeManagement}>
          <section
            className={`management-modal${managementModal === "kanban" ? " management-modal-kanban" : ""}`}
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
                  {managementModal === "kanban" && "Kanban du projet"}
                  {managementModal === "agents" && "Agents"}
                  {managementModal === "skills" && "Skills"}
                  {managementModal === "providers" && "Providers"}
                  {managementModal === "settings" && "Paramètres"}
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
                  ><Icon name="add" size="sm" /></button>
                )}
                <button onClick={closeManagement}
                  disabled={Boolean(
                    cronEditor && (cronWorkflowMutationBusy || savingResource || testingCron)
                  )}
                  aria-label="Fermer"><Icon name="close" size="sm" /></button>
              </div>
            </header>

            {managementError && (
              <div className="management-error" role="alert">{managementError}</div>
            )}

            {/* Une hiérarchie invalide n'empêche plus le catalogue de charger :
                la délégation fautive est ignorée et signalée ici, là même où
                elle se corrige — et le bouton la corrige vraiment, sinon le
                seul remède serait d'éditer un fichier hors de l'application. */}
            {managementModal === "agents" && !resourceEditor
              && (catalog?.agent_warnings?.length ?? 0) > 0 && (
              <div className="management-warning" role="status">
                {catalog?.agent_warnings?.map((warning) => (
                  <p key={warning.agent_id}>
                    <strong>{warning.message}</strong> {warning.remedy}
                  </p>
                ))}
                <div className="checkbox-field-actions">
                  <button
                    type="button"
                    onClick={() => void repairAgentHierarchy()}
                    disabled={repairingHierarchy}
                  >
                    {repairingHierarchy ? "Correction…" : "Corriger"}
                  </button>
                </div>
              </div>
            )}

            {managementModal === "settings" && (
              <ModuleSettingsPanel
                modules={moduleSettings}
                saving={savingResource}
                onChange={updateModuleSetting}
                onSave={(module) => void saveModuleSettings(module)}
                header={
                  <section className="content-home">
                  <h3>Dossier de données</h3>
                  <p>
                    Emplacement de <code>content-agents/</code> — agents, skills,
                    sessions, secrets et base d’état. Indique le dossier parent,
                    ou un <code>content-agents</code> existant. Le kernel l’ouvre
                    au démarrage : un changement prend effet au suivant, et aucun
                    fichier n’est déplacé.
                  </p>
                  <form
                    className="workspace-form modal-workspace-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveContentHome(contentHomeInput);
                    }}
                  >
                    <input
                      value={contentHomeInput}
                      onChange={(event) => setContentHomeInput(event.target.value)}
                      placeholder={contentHome?.default || "/chemin/du/dossier"}
                      aria-label="Dossier de données"
                      disabled={contentHomeBusy}
                    />
                    <button
                      type="button"
                      className="pick-workspace"
                      disabled={contentHomeBusy}
                      onClick={() => void pickContentHome()}
                      aria-label="Choisir un dossier"
                      title="Choisir un dossier"
                    >
                      <Icon name="folderOpen" size="sm" />
                    </button>
                    <button
                      className="add-workspace"
                      disabled={contentHomeBusy}
                      aria-label="Enregistrer l’emplacement"
                      title="Enregistrer"
                    >
                      <Icon
                        name={contentHomeBusy ? "running" : "save"}
                        size="sm"
                        className={contentHomeBusy ? "spin" : undefined}
                      />
                    </button>
                  </form>
                  <dl className="content-home-facts">
                    <dt>Utilisé actuellement</dt>
                    <dd>{contentHome?.current || "…"}</dd>
                    {contentHome?.environment_override && (
                      <>
                        <dt>Forcé par l’environnement</dt>
                        {/* Sans cette ligne, on chercherait longtemps pourquoi
                            le réglage enregistré ne s'applique jamais. */}
                        <dd>
                          AMK_HOME = {contentHome.environment_override} — ce
                          réglage reste sans effet tant que la variable existe.
                        </dd>
                      </>
                    )}
                  </dl>
                  {contentHomeNotice && <p className="content-home-notice">{contentHomeNotice}</p>}
                  {contentHomeError && <p className="content-home-error">{contentHomeError}</p>}
                  </section>
                }
              />
            )}

            {resourceEditor ? (
              <ResourceEditorForm
                editor={resourceEditor}
                catalog={catalog}
                savingResource={savingResource}
                avatarInput={avatarInput}
                agentHasAvatar={agentHasAvatar}
                avatarVersion={avatarVersion}
                avatarBusy={avatarBusy}
                providerModels={providerModels}
                modelsLoading={modelsLoading}
                updateResourceId={updateResourceId}
                updateEditorField={updateEditorField}
                toggleEditorListField={toggleEditorListField}
                setEditorListValues={setEditorListValues}
                updateTelegramField={updateTelegramField}
                uploadAgentAvatar={uploadAgentAvatar}
                removeAgentAvatar={removeAgentAvatar}
                setResourceEditor={setResourceEditor}
                onSave={() => void saveResource()}
              />
            ) : managementModal === "kanban" ? (
              <KanbanBoard
                workspace={activeWorkspace}
                onUsePrompt={(value) => {
                  setPrompt(value);
                  closeManagement();
                  requestAnimationFrame(() => textarea.current?.focus());
                }}
              />
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
                    <Icon
                      name={pickingWorkspace ? "running" : "folderOpen"}
                      size="sm"
                      className={pickingWorkspace ? "spin" : undefined}
                    />
                  </button>
                  <button className="add-workspace" disabled={!workspaceInput.trim()} aria-label="Ajouter le projet">
                    <Icon name="add" size="sm" />
                  </button>
                </form>
                {workspaceError && <small className="workspace-error">{workspaceError}</small>}
                <div className="management-list">
                  {/* Travailler sans projet est un choix, pas un manque : c'est
                      la vue de l'agent lui-même — son canal et son espace
                      personnel. Elle mérite donc une ligne, au même rang que
                      les projets. */}
                  <div className={`management-row ${activeWorkspace === "" ? "active" : ""}`}>
                    <button onClick={() => { chooseWorkspace(""); setManagementModal(null); }}>
                      <span className="row-status" />
                      <span>
                        <strong>Aucun projet</strong>
                        <small>Canal de l’agent et espace personnel</small>
                      </span>
                      {activeWorkspace === "" && <em>Actif</em>}
                    </button>
                  </div>
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
                      ><Icon name="close" size="sm" /></button>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {!resourceEditor && managementModal === "agents" && (
              <div className="management-body management-list">
                {/* Orchestrateurs d'abord, sous-agents ensuite, en retrait.
                    Le retrait seul se lisait comme un rattachement au dernier
                    orchestrateur listé : l'intitulé dit qu'ils appartiennent à
                    tous, ce qui est la règle réelle. */}
                {[...managedResources]
                  .sort((left, right) => Number(left.subagent) - Number(right.subagent))
                  .map((resource, index, tries) => (
                  <Fragment key={resource.id}>
                  {resource.subagent && !tries[index - 1]?.subagent && (
                    <p className="resource-group">Sous-agents partagés</p>
                  )}
                  <div
                    className={`resource-row ${resource.id === agentId ? "active" : ""}`}
                    data-subagent={resource.subagent ? "true" : undefined}
                  >
                    <button
                      className="resource-select"
                      onClick={() => { setAgentId(resource.id); setManagementModal(null); }}
                    >
                      <span className="context-icon agent-icon">{resource.id.slice(0, 1).toUpperCase()}</span>
                      <span>
                        <strong>
                          {resource.id}
                          {resource.subagent && <em className="resource-tag">sous-agent</em>}
                        </strong>
                        <small>{resource.description}</small>
                      </span>
                    </button>
                    <div className="resource-actions">
                      <button
                        onClick={() => editResource("agents", resource)}
                        aria-label={`Éditer ${resource.id}`}
                      ><Icon name="edit" size="sm" /></button>
                      <button
                        disabled={resource.id === "main"}
                        onClick={() => void deleteResource("agents", resource.id)}
                        aria-label={`Supprimer ${resource.id}`}
                      ><Icon name="remove" size="sm" /></button>
                    </div>
                  </div>
                  </Fragment>
                ))}
              </div>
            )}

            {!resourceEditor && managementModal === "skills" && (
              <div className="management-body management-list">
                {/* Pas d'activation ici : une skill se charge en permanence via la
                    configuration de l'agent, ou ponctuellement via sa commande.
                    Une troisième voie sans trace était la source de confusion. */}
                {managedResources.map((resource) => (
                  <div className="resource-row" key={resource.id}>
                    <span className="resource-label">
                      <strong>
                        {resource.id}
                        {agentSkills.includes(resource.id) && (
                          <em className="resource-tag">préchargée par {agentId}</em>
                        )}
                      </strong>
                      <small>{resource.description}</small>
                    </span>
                    <div className="resource-actions">
                      <button
                        onClick={() => editResource("skills", resource)}
                        aria-label={`Éditer ${resource.id}`}
                      ><Icon name="edit" size="sm" /></button>
                      <button
                        onClick={() => void deleteResource("skills", resource.id)}
                        aria-label={`Supprimer ${resource.id}`}
                      ><Icon name="remove" size="sm" /></button>
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
              <ProviderEditor
                providerEditor={providerEditor}
                setProviderEditor={setProviderEditor}
                savingResource={savingResource}
                codexConnected={codexConnected}
                codexAuthLoading={codexAuthLoading}
                selectProviderKind={selectProviderKind}
                connectCodex={() => void connectCodex()}
                disconnectCodex={() => void disconnectCodex()}
                onSave={() => void saveProvider()}
              />
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
                      ><Icon name="edit" size="sm" /></button>
                      <button
                        disabled={provider.id === defaultProvider}
                        onClick={() => void deleteProvider(provider.id)}
                        aria-label={`Supprimer ${provider.id}`}
                      ><Icon name="remove" size="sm" /></button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {managementModal === "crons" && cronEditor && (
              <RoutineEditor
                routines={routines}
                savingResource={savingResource}
                sessions={sessions}
                catalog={catalog}
                securityMode={securityMode}
                workspaces={workspaces}
                onSave={() => void saveCron()}
              />
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
                        <small>{describeCron(cronEditorFromJob(job))} · {job.agent_id} · {job.workspace || "espace personnel"}</small>
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
                        title="Lancer maintenant" aria-label="Lancer maintenant"><Icon name="play" size="sm" /></button>
                      <button onClick={() => void openCronEditor(job)}
                        title="Éditer" aria-label="Éditer"><Icon name="edit" size="sm" /></button>
                      <button onClick={() => void deleteCron(job.id)} title="Supprimer" aria-label="Supprimer">
                        <Icon name="remove" size="sm" />
                      </button>
                    </div>
                  </div>
                ))}
                {cronJobs.length === 0 && (
                  <div className="management-empty">
                    <Icon name="automation" size="xl" />
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
          {/* Pas de bouton d'ouverture ici : quand le panneau est replié, le
              rail d'icônes prend sa place et porte déjà cette action. */}
          {/* Agent et projet côte à côte plutôt qu'empilés, et cliquables :
              ce sont deux réglages, pas un titre. Le nom du projet suffit —
              le chemin complet mangeait la barre et n'apprend rien. */}
          <div className="topbar-title">
            <button
              type="button"
              className="topbar-chip"
              onClick={() => openManagement("agents")}
              data-tip="Changer d’agent"
              data-tip-side="bottom"
            >
              <Icon name="agent" size="sm" />
              <span>{activeAgent?.id || "main"}</span>
            </button>

            <button
              type="button"
              className={[
                "topbar-chip",
                // La conversation ouverte peut ne pas appartenir au projet
                // sélectionné : un canal permanent écrit dans l'espace personnel
                // de son agent quel que soit le projet affiché dans le rail. Sans
                // marque, on croit travailler dans le projet — un jeu entier
                // s'est écrit dans `workspaces/main` pendant que le rail
                // indiquait `test9`.
                conversationWorkspace !== activeWorkspace ? "topbar-chip-divergent" : "",
              ].filter(Boolean).join(" ")}
              onClick={() => openManagement("projects")}
              data-tip={
                conversationWorkspace !== activeWorkspace
                  ? `Cette conversation écrit dans ${conversationWorkspace || "l’espace personnel de l’agent"}, pas dans le projet sélectionné.`
                  : conversationWorkspace || "Aucun projet associé"
              }
              data-tip-side="bottom"
            >
              <Icon name="project" size="sm" />
              <span>
                {activeSession?.workspace_kind === "agent_default" || isAgentChannel
                  ? "Espace personnel"
                  // Le projet de la conversation prime sur celui sélectionné :
                  // rouvrir une session de l'historique ne doit pas afficher le
                  // projet courant à la place du sien.
                  : conversationWorkspaceInfo?.name
                    || (conversationWorkspace ? conversationWorkspace.split(/[\\/]/).pop() : "")
                    || "Aucun projet"}
              </span>
            </button>
          </div>

          <div className="topbar-actions">
            {gitSnapshot && (
              <div className="topbar-group">
                <GitToolbar
                  snapshot={gitSnapshot}
                  branches={gitBranches}
                  disabled={running || gitBusy}
                  onSwitch={(branch) => void switchGitBranch(branch)}
                  onValidate={() => void proposeGitCommit()}
                />
              </div>
            )}

            {/* Le modèle actif est déjà affiché dans le composer, à l'endroit
                où il se change. */}
            <button
              type="button"
              className="ibtn"
              data-tip="Nouvelle conversation"
              data-tip-side="bottom-end"
              aria-label="Nouvelle conversation"
              onClick={newConversation}
              disabled={running}
            >
              <Icon name="newChat" size="md" />
            </button>

            <span className="topbar-sep" />

            <button
              type="button"
              className="ibtn"
              data-tip={panels.right.open ? "Fermer le panneau droit" : "Ouvrir le panneau droit"}
              data-tip-side="left"
              aria-label="Basculer le panneau droit"
              aria-pressed={panels.right.open}
              onClick={() => panels.toggle("right")}
            >
              <Icon name={panels.right.open ? "panelRightClose" : "panelRightOpen"} size="md" />
            </button>
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
                  <article className={`message ${message.role} ${message.error ? "error" : ""}`}>
                    {message.role === "user" ? (
                      <div className="message-avatar">X</div>
                    ) : (
                      <AgentAvatar
                        agentId={activeAgent?.id}
                        label={activeAgent?.id || "Agent"}
                        hasAvatar={activeAgent?.has_avatar}
                        version={avatarVersion}
                      />
                    )}
                    <div>
                      <div className="message-meta">
                        <strong>{message.role === "user" ? "Toi" : activeAgent?.id || "Agent"}</strong>
                        <span>{message.meta}</span>
                      </div>
                      {message.role === "assistant" ? (
                        <>
                          {/* Le déroulé appartient à la réponse, pas à la
                              demande : placé avant l'article il s'affichait
                              sous le message de l'utilisateur, qui semblait
                              alors avoir exécuté les outils lui-même. */}
                          {message.runId
                            && traceEventsForRun(traceEvents, message.runId).length > 0 && (
                              <ProcessTrace
                                events={traceEventsForRun(traceEvents, message.runId)}
                                live={running && activeRunId === message.runId}
                              />
                            )}
                          <MarkdownMessage content={message.content} />
                          <MessageArtifacts
                            sessionId={activeSessionId}
                            artifacts={message.artifacts}
                            onImageOpen={setImagePreview}
                          />
                          <RunCost stats={runStats} runId={message.runId} />
                          {gitSnapshotForRun(traceEvents, message.runId) && (
                            <GitChangeCard
                              snapshot={gitSnapshotForRun(traceEvents, message.runId)!}
                              onOpen={(file) => openGitReview(
                                gitSnapshotForRun(traceEvents, message.runId)!, file,
                              )}
                            />
                          )}
                        </>
                      ) : (
                        <>
                          <UserMessageImages
                            images={message.images}
                            onImageOpen={setImagePreview}
                          />
                          <p>{message.content}</p>
                        </>
                      )}
                    </div>
                  </article>
                </Fragment>
              ))}
              {running &&
                !messages.some((message) => message.role === "assistant" && message.runId === activeRunId) && (
                <ProcessTrace
                  events={activeRunId ? traceEventsForRun(traceEvents, activeRunId) : []}
                  live
                />
              )}
              {running && (
                <article className="message assistant thinking">
                  <AgentAvatar
                    agentId={activeAgent?.id}
                    label={activeAgent?.id || "Agent"}
                    hasAvatar={activeAgent?.has_avatar}
                    version={avatarVersion}
                  />
                  <div>
                    {compacting ? (
                      <CompactionIndicator />
                    ) : (
                      <div className="pulse"><i /><i /><i /></div>
                    )}
                  </div>
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
          {activeSkillCount > 0 && (
            <div className="selected-skills">
              {agentSkills.map((skill) => (
                <span
                  className="skill-chip"
                  key={skill}
                  title={`Fournie par l'agent ${agentId}`}
                >
                  {skill}
                </span>
              ))}
              {extraSkills.map((skill) => (
                <button
                  type="button"
                  className="skill-chip skill-chip-session skill-chip-removable"
                  key={skill}
                  onClick={() => toggleSkill(skill)}
                  aria-label={`Retirer la skill ${skill}`}
                >
                  {skill}<span aria-hidden="true">×</span>
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
                  ><Icon name="close" size="xs" /></button>
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
            canSend={Boolean(prompt.trim()) && !running && !clearingSession}
            vision={Boolean(activeProvider?.vision)}
            soundEnabled={soundEnabled}
            onSoundToggle={() => setSoundEnabled((actif) => !actif)}
            knowledgeMode={knowledgeMode}
            knowledgeCount={knowledgeSelection.length}
            onKnowledgeModeChange={(mode) => {
              setKnowledgeMode(mode);
              // Passer en manuel sans rien avoir choisi n'injecterait rien :
              // autant ouvrir la sélection tout de suite.
              if (mode === "manual") void openKnowledge();
            }}
            onKnowledgeSelect={() => void openKnowledge()}
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

      <Dock
        tabs={dockTabs}
        activeTab={dockTab}
        onTabChange={(id) => setDockTab(id as DockTabId)}
        collapsed={!panels.right.open}
        title={dockTab === "git" ? "Révision" : "Fichiers"}
        subtitle={
          dockTab === "git"
            ? gitReviewSnapshot
              ? `${gitReviewSnapshot.branch || "—"} · ${gitReviewSnapshot.files.length} fichier(s)`
              : gitSnapshot?.branch || undefined
            : activeWorkspaceInfo?.name
        }
        actions={
          <>
            {/* Le commit se décide en lisant les diffs : le bouton doit être là,
                pas seulement dans la barre du haut. Le message est proposé par
                le modèle depuis le patch, puis relu avant validation. */}
            {dockTab === "git" && (gitReviewSnapshot || gitSnapshot)?.files.length ? (
              <button
                type="button"
                className="dock-commit"
                disabled={running || gitBusy}
                data-tip="Rédige un message à partir des modifications"
                data-tip-side="left"
                onClick={() => void proposeGitCommit()}
              >
                <Icon name="git" size="sm" />
                {gitBusy ? "Rédaction…" : "Commit"}
              </button>
            ) : null}
            <button
              type="button"
              className="ibtn sm"
              data-tip="Fermer"
              data-tip-side="left"
              aria-label="Fermer le panneau droit"
              onClick={() => panels.setOpen("right", false)}
            >
              <Icon name="close" size="sm" />
            </button>
          </>
        }
        resizer={
          <Resizer
            side="start"
            label="Largeur du panneau droit"
            active={panels.resizing === "right"}
            onPointerDown={panels.startResize("right")}
            onKeyDown={panels.nudge("right")}
          />
        }
      >
        {dockTab === "git" ? (
          <GitReviewBody
            snapshot={gitReviewSnapshot || gitSnapshot}
            selected={gitSelectedFile}
            onSelect={setGitSelectedFile}
          />
        ) : explorerFile ? (
          <div className="git-review-body" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
            <section>
              <h3>
                {explorerFile.path}
                <button
                  type="button"
                  className="ibtn sm"
                  style={{ float: "right", marginTop: -6 }}
                  aria-label="Revenir à l'arborescence"
                  onClick={() => setExplorerFile(null)}
                >
                  <Icon name="close" size="xs" />
                </button>
              </h3>
              <pre>{explorerFile.content}</pre>
            </section>
          </div>
        ) : explorerError ? (
          <div className="git-review-body" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
            <section>
              <h3>
                Aperçu indisponible
                <button
                  type="button"
                  className="ibtn sm"
                  style={{ float: "right", marginTop: -6 }}
                  aria-label="Revenir à l'arborescence"
                  onClick={() => setExplorerError("")}
                >
                  <Icon name="close" size="xs" />
                </button>
              </h3>
              <ChromeEmpty icon="error">{explorerError}</ChromeEmpty>
            </section>
          </div>
        ) : (
          <FileExplorer
            workspace={conversationWorkspace || undefined}
            kernelUrl={fileExplorerKernelUrl}
            changedPaths={changedPaths}
            onOpenFile={(path) => void openExplorerFile(path)}
          />
        )}
      </Dock>

      {knowledgeOpen && (
        <div className="management-backdrop" onClick={() => setKnowledgeOpen(false)}>
          <section
            className="management-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="knowledge-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header>
              <div>
                <p className="eyebrow">Bibliothèque de {agentId}</p>
                <h2 id="knowledge-title">Pages à joindre</h2>
              </div>
              <div className="modal-header-actions">
                <button onClick={() => setKnowledgeOpen(false)} aria-label="Fermer">
                  <Icon name="close" size="sm" />
                </button>
              </div>
            </header>
            {knowledgeError && <div className="management-error">{knowledgeError}</div>}
            <div className="management-body management-list">
              {knowledgePages.length > 0 && (
                <div className="checkbox-field-actions">
                  <button
                    type="button"
                    onClick={() => setKnowledgeSelection(knowledgePages.map((page) => page.slug))}
                  >Tout sélectionner</button>
                  <button type="button" onClick={() => setKnowledgeSelection([])}>Aucune</button>
                </div>
              )}
              {knowledgePages.map((page) => (
                <div className="resource-row" key={page.slug}>
                  <label className="resource-select">
                    <input
                      type="checkbox"
                      checked={knowledgeSelection.includes(page.slug)}
                      onChange={() => setKnowledgeSelection((current) =>
                        current.includes(page.slug)
                          ? current.filter((slug) => slug !== page.slug)
                          : [...current, page.slug],
                      )}
                    />
                    <span>
                      <strong>
                        {page.title}
                        {page.tags.map((tag) => (
                          <em className="resource-tag" key={tag}>{tag.trim()}</em>
                        ))}
                      </strong>
                      <small>
                        {/* Le poids est affiché parce qu'il se paie : une
                            sélection large peut saturer la fenêtre. */}
                        {page.ingested || "sans date"} · {Math.max(1, Math.round(page.bytes / 1024))} ko
                        {page.summary ? ` · ${page.summary}` : ""}
                      </small>
                    </span>
                  </label>
                </div>
              ))}
              {knowledgePages.length === 0 && !knowledgeError && (
                <div className="management-empty">
                  <strong>Bibliothèque vide</strong>
                  <p>Utilise <code>/save</code> pour y archiver une recherche.</p>
                </div>
              )}
            </div>
          </section>
        </div>
      )}

      {gitCommitMessage !== null && (
        <GitCommitDialog
          message={gitCommitMessage}
          busy={gitBusy}
          error={gitError}
          source={gitCommitSource}
          onMessage={setGitCommitMessage}
          onCancel={() => { setGitCommitMessage(null); setGitError(""); }}
          onCommit={() => void commitGitChanges()}
        />
      )}
      {gitError && gitCommitMessage === null && (
        <button type="button" className="git-toast" onClick={() => setGitError("")}>{gitError}</button>
      )}
    </Shell>
  );
}
