/**
 * MODÈLE DE LA SURFACE WEB
 * -----------------------------------------------------------------------------
 * Les formes que l'interface manipule, et les fonctions pures qui les
 * transforment. Rien ici ne connaît React : ce fichier se lit sans dérouler
 * `page.tsx`, et se modifie sans risquer d'y toucher.
 *
 * La frontière est nette : un type, une constante ou une fonction sans état
 * appartient ici; tout ce qui rend ou tient un état reste dans un composant.
 */

import { parse as parseYaml, stringify as stringifyYaml } from "yaml";

import type { SecurityMode } from "../components/composer-controls";
import type { GitSnapshot } from "../components/git-workspace";
import type { TraceEvent } from "./trace";

export type DockTabId = "git" | "files";

export type Agent = {
  id: string;
  description: string;
  provider: string;
  model: string | null;
  skills: string[];
  delegates: string[];
  has_avatar?: boolean;
  /** Niveau appliqué par les surfaces sans sélecteur, et défaut du composer. */
  security_mode?: SecurityMode;
  /** Exécutant appelé par un orchestrateur; il ne délègue à personne. */
  subagent?: boolean;
};

export type Skill = {
  name: string;
  description: string;
  /** Outils que la skill réclame; ils n'ont d'effet qu'inscrits dans l'agent. */
  tools?: string[];
};

/**
 * Outils imposés par les skills cochées d'un agent.
 *
 * Une skill ne peut rien autoriser d'elle-même : `allowed-tools` n'est qu'une
 * phrase ajoutée aux instructions. Sans cette jonction, cocher une skill qui
 * réclame `knowledge_ingest` sur un agent qui ne l'a pas produisait un modèle
 * qui cherche un outil absent et improvise — un échec qui ne se manifeste qu'à
 * l'exécution, loin de la case cochée.
 */
export function toolsRequiredBySkills(
  skills: Skill[],
  selected: string[],
  known: string[],
): string[] {
  const retenues = new Set(selected);
  const existants = new Set(known);
  return [
    ...new Set(
      skills
        .filter((skill) => retenues.has(skill.name))
        .flatMap((skill) => skill.tools || [])
        .filter((tool) => existants.has(tool)),
    ),
  ];
}
export type SlashCommand = {
  command: string;
  description: string;
  kind: string;
  skill: string;
  source: string;
};
/** Traitée par la surface, jamais transmise au modèle : elle purge le journal
 *  de la session côté kernel. */
export const routineClearCommand: SlashCommand = {
  command: "/clear",
  description: "Vider définitivement le fil de cette conversation.",
  kind: "native",
  skill: "",
  source: "interface",
};
export type Catalog = {
  default_provider: string;
  /** Délégations écartées au chargement — signalées, jamais bloquantes. */
  agent_warnings?: {
    agent_id: string;
    source: string;
    dropped: string[];
    message: string;
    remedy: string;
  }[];
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
  modules: { id: string; name: string; description: string }[];
  configurable_modules: { id: string; name: string }[];
};

export type ModuleSettingField = {
  name: string;
  label: string;
  type: "text" | "secret" | "file" | "directory" | "integer" | "number" | "boolean" | "select" | "string_list";
  description?: string | null;
  required: boolean;
  default?: unknown;
  options: string[];
  minimum?: number | null;
  maximum?: number | null;
  configured?: boolean;
  value?: unknown;
};

export type ConfigurableModule = {
  id: string;
  name: string;
  title: string;
  description: string;
  applies_to: string[];
  state: "configured" | "defaults" | "required";
  fields: ModuleSettingField[];
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  images?: ComposerImage[];
  meta?: string;
  error?: boolean;
  runId?: string;
  artifacts?: RunArtifact[];
};

export type RunArtifact = {
  artifact_id: string;
  name: string;
  media_type: string;
  kind: string;
  bytes?: number;
};

export type Workspace = {
  path: string;
  name: string;
  readable: boolean;
  writable: boolean;
};

export type ComposerImage = {
  id: string;
  name: string;
  mediaType: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  dataUrl: string;
};

export const SUPPORTED_IMAGE_TYPES = [
  "image/png", "image/jpeg", "image/webp", "image/gif",
] as const satisfies readonly ComposerImage["mediaType"][];

/**
 * Le type déclaré par un artefact vient du serveur : c'est une chaîne libre.
 * L'affecter directement au type restreint revenait à affirmer une garantie que
 * personne ne vérifie. On retombe sur le PNG, que tous les navigateurs affichent.
 */
export function toImageMediaType(value: string): ComposerImage["mediaType"] {
  return SUPPORTED_IMAGE_TYPES.find((item) => item === value) ?? "image/png";
}

export type ImagePreview = {
  source: string;
  name: string;
};

export type ManagedResource = {
  id: string;
  description: string;
  content: string;
  telegram?: TelegramAgentConfig;
  has_avatar?: boolean;
  subagent?: boolean;
};

export type TelegramAgentConfig = {
  enabled: boolean;
  hide_session: boolean;
  user_id: string;
  bot_token: string;
  user_id_configured?: boolean;
  bot_token_configured?: boolean;
};

export type ResourceEditor = {
  kind: "agents" | "skills";
  id: string;
  frontmatter: Record<string, unknown>;
  body: string;
  creating: boolean;
  telegram?: TelegramAgentConfig;
};

export type ManagedProvider = {
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
  models_dir?: string | null;
  server_binary?: string | null;
  port?: number;
  n_gpu_layers?: number;
  num_ctx?: number;
  flash_attn?: boolean;
  preserve_thinking?: boolean;
  reasoning_budget?: number;
  startup_timeout_seconds?: number;
  llama_args?: string[];
  temperature?: number;
  top_k?: number;
  top_p?: number;
  num_predict?: number;
};

export const codexAuthHelpUrl = "https://learn.chatgpt.com/docs/auth?surface=cli";
export const codexApiUrl = "https://chatgpt.com/backend-api/codex";
export const providerKindLabels: Record<string, string> = {
  "openai-codex": "Codex — connexion ChatGPT",
  openai: "OpenAI API — clé API",
  deepseek: "DeepSeek — clé API",
  qwen: "Qwen — clé API",
  "llama-cpp": "llama.cpp — local",
  "claude-oauth": "Claude — connexion OAuth",
  anthropic: "Anthropic API — clé API",
};

/**
 * Le modèle ne figure volontairement pas ici.
 *
 * Il appartient à l'agent, qui en fait son défaut; le sélecteur du composer ne
 * sert qu'à en changer ponctuellement. Persisté, ce choix ponctuel masquait
 * définitivement le défaut de l'agent — on pouvait lire `deepseek-v4-pro` dans
 * la configuration de `main` et voir `deepseek-v4-flash` dans le composer, sans
 * rien pour expliquer l'écart.
 */
export type KnowledgePage = {
  slug: string;
  title: string;
  tags: string[];
  ingested: string;
  source: string;
  summary: string;
  bytes: number;
};

export type ContentHome = {
  /** Dossier réellement utilisé par le kernel en cours d'exécution. */
  current: string;
  /** Choix enregistré, ou null tant que l'emplacement par défaut s'applique. */
  configured: string | null;
  default: string;
  /** `AMK_HOME` l'emporte sur le réglage : le taire rendrait l'écran menteur. */
  environment_override: string | null;
};

export type ComposerPreferences = {
  securityMode: "safe" | "limited" | "power";
  providerId: string;
  reasoning: "minimal" | "low" | "medium" | "high" | "xhigh";
  /** Signal sonore en fin de run. Un run long se surveille mal des yeux. */
  soundEnabled: boolean;
};

export const composerPreferencesKey = "amk.composer.preferences.v1";

export function parseMarkdownResource(content: string) {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) return { frontmatter: {}, body: content };
  return {
    frontmatter: (parseYaml(match[1]) || {}) as Record<string, unknown>,
    body: match[2].replace(/\s+$/, ""),
  };
}

export function buildMarkdownResource(editor: ResourceEditor) {
  const frontmatter = {
    ...editor.frontmatter,
    [editor.kind === "agents" ? "id" : "name"]: editor.id,
  };
  return `---\n${stringifyYaml(frontmatter).trimEnd()}\n---\n${editor.body.trimEnd()}\n`;
}

export function resourceList(value: unknown) {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") return value.split(",").map((item) => item.trim()).filter(Boolean);
  return [];
}

export function parseListFieldValue(value: string) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

export type SessionSummary = {
  session_id: string;
  agent_id: string;
  prompt: string;
  workspace: string | null;
  effective_workspace?: string;
  workspace_kind?: "project" | "agent_default";
  created_at: string;
  updated_at: string;
  status: string;
  output: string | null;
  errors: { message: string }[];
  event_count: number;
  trigger?: "user" | "resume" | "cron" | "cron_resume" | "cron_test" | "agent_channel" | "telegram";
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

export function gitSnapshotForRun(events: TraceEvent[], runId?: string): GitSnapshot | null {
  if (!runId) return null;
  const event = [...events].reverse().find((item) =>
    item.run_id === runId && item.type === "git.snapshot"
  );
  if (!event) return null;
  const payload = event.payload as Partial<GitSnapshot>;
  return payload.available && Array.isArray(payload.files) ? payload as GitSnapshot : null;
}
