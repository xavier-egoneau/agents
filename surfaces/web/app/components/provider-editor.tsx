"use client";

/**
 * ÉDITEUR DE PROVIDER
 * -----------------------------------------------------------------------------
 * Le formulaire d'un fournisseur de modèles, sorti de `page.tsx` sans changer
 * sa logique. Il ne tient aucun état : `page.tsx` garde `providerEditor` et les
 * actions, ce composant se contente de les rendre.
 */

import type { Dispatch, SetStateAction } from "react";

import {
  codexApiUrl,
  codexAuthHelpUrl,
  parseListFieldValue,
  providerKindLabels,
  type ManagedProvider,
} from "../lib/model";

/** L'édition ajoute un drapeau de création au provider lui-même. */
type ProviderDraft = ManagedProvider & { creating: boolean };

type ProviderEditorProps = {
  providerEditor: ProviderDraft;
  setProviderEditor: Dispatch<SetStateAction<ProviderDraft | null>>;
  savingResource: boolean;
  /** État de la connexion OAuth Codex, tenu par `page.tsx`. */
  codexConnected: boolean;
  codexAuthLoading: boolean;
  selectProviderKind: (kind: string) => void;
  connectCodex: () => void;
  disconnectCodex: () => void;
  onSave: () => void;
};

export function ProviderEditor({
  providerEditor,
  setProviderEditor,
  savingResource,
  codexConnected,
  codexAuthLoading,
  selectProviderKind,
  connectCodex,
  disconnectCodex,
  onSave,
}: ProviderEditorProps) {
  return (
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
            {["deepseek", "qwen", "llama-cpp", "openai-codex", "claude-oauth", "openai", "anthropic"].map((kind) => (
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
        {providerEditor.kind === "llama-cpp" && (
          <>
            <label className="field-wide">
              Dossier des modèles GGUF
              <input
                value={providerEditor.models_dir || ""}
                placeholder="C:\\Users\\vous\\llama.cpp\\models"
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, models_dir: event.target.value || null,
                }))}
              />
            </label>
            <label className="field-wide">
              Exécutable llama-server
              <input
                value={providerEditor.server_binary || ""}
                placeholder="llama-server (PATH) ou chemin absolu"
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, server_binary: event.target.value || null,
                }))}
              />
            </label>
            <label>
              Port local
              <input
                type="number"
                min={1}
                max={65535}
                value={providerEditor.port || 8123}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, port: Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Fenêtre de contexte
              <input
                type="number"
                min={1}
                value={providerEditor.num_ctx || 16384}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, num_ctx: Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Couches GPU
              <input
                type="number"
                value={providerEditor.n_gpu_layers ?? 999}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, n_gpu_layers: Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Timeout de démarrage
              <input
                type="number"
                min={1}
                value={providerEditor.startup_timeout_seconds || 240}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, startup_timeout_seconds: Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Température
              <input
                type="number"
                min={0}
                step={0.1}
                value={providerEditor.temperature ?? ""}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current,
                  temperature: event.target.value === ""
                    ? undefined : Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Top K
              <input
                type="number"
                min={0}
                value={providerEditor.top_k ?? ""}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current,
                  top_k: event.target.value === ""
                    ? undefined : Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Budget de raisonnement
              <input
                type="number"
                min={-1}
                value={providerEditor.reasoning_budget ?? ""}
                placeholder="16384"
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current,
                  reasoning_budget: event.target.value === ""
                    ? undefined : Number(event.target.value),
                }))}
              />
            </label>
            <label>
              Tokens de sortie maximum
              <input
                type="number"
                min={1}
                value={providerEditor.num_predict ?? ""}
                placeholder="32768"
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current,
                  num_predict: event.target.value === ""
                    ? undefined : Number(event.target.value),
                }))}
              />
            </label>
            <label className="provider-vision">
              <input
                type="checkbox"
                checked={providerEditor.flash_attn ?? true}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, flash_attn: event.target.checked,
                }))}
              />
              Flash attention
            </label>
            <label className="provider-vision">
              <input
                type="checkbox"
                checked={providerEditor.preserve_thinking ?? false}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current, preserve_thinking: event.target.checked,
                }))}
              />
              Conserver le raisonnement entre les tours
            </label>
            <label className="field-wide">
              Arguments llama-server (un par ligne)
              <textarea
                value={(providerEditor.llama_args || []).join("\n")}
                onChange={(event) => setProviderEditor((current) => current && ({
                  ...current,
                  llama_args: event.target.value.split(/\r?\n/).filter(Boolean),
                }))}
                placeholder={"--n-cpu-moe\n21"}
              />
            </label>
          </>
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
          onClick={() => void onSave()}
        >
          {savingResource ? "Enregistrement…" : "Enregistrer"}
        </button>
      </div>
    </div>
  );
}
