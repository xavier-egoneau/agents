"use client";

/**
 * ÉDITEUR D'AGENT ET DE SKILL
 * -----------------------------------------------------------------------------
 * Un seul formulaire pour les deux : ils partagent `resourceEditor`, leur
 * front matter et leur corps markdown. Les champs propres aux agents — avatar,
 * provider, niveau, délégations, passerelle Telegram — apparaissent selon le
 * `kind`, comme avant l'extraction.
 *
 * Aucun état ici : `page.tsx` garde l'édition en cours et les actions, ce
 * composant les rend.
 */

import type { Dispatch, RefObject, SetStateAction } from "react";

import { AgentAvatar } from "./agent-avatar";
import { ToolSelector } from "./tool-selector";
import {
  resourceList,
  toolsRequiredBySkills,
  type Catalog,
  type ResourceEditor,
  type TelegramAgentConfig,
} from "../lib/model";

type ResourceEditorFormProps = {
  editor: ResourceEditor;
  catalog: Catalog | null;
  savingResource: boolean;
  avatarInput: RefObject<HTMLInputElement | null>;
  agentHasAvatar: (agentId: string) => boolean;
  /** Change à chaque envoi d'avatar : force le navigateur à relire l'image. */
  avatarVersion: number;
  avatarBusy: boolean;
  providerModels: Record<string, string[]>;
  modelsLoading: boolean;
  updateResourceId: (value: string, normalize?: boolean) => void;
  updateEditorField: (name: string, value: unknown) => void;
  toggleEditorListField: (name: string, value: string, defaults?: string[]) => void;
  setEditorListValues: (
    name: string,
    values: string[],
    next: boolean,
    defaults?: string[],
  ) => void;
  updateTelegramField: (name: keyof TelegramAgentConfig, value: string | boolean) => void;
  uploadAgentAvatar: (agentId: string, file: File) => void;
  removeAgentAvatar: (agentId: string) => void;
  setResourceEditor: Dispatch<SetStateAction<ResourceEditor | null>>;
  onSave: () => void;
};

export function ResourceEditorForm({
  editor: resourceEditor,
  catalog,
  savingResource,
  avatarInput,
  agentHasAvatar,
  avatarVersion,
  avatarBusy,
  providerModels,
  modelsLoading,
  updateResourceId,
  updateEditorField,
  toggleEditorListField,
  setEditorListValues,
  updateTelegramField,
  uploadAgentAvatar,
  removeAgentAvatar,
  setResourceEditor,
  onSave,
}: ResourceEditorFormProps) {
  // L'absence de clé `tools` vaut « tous les outils » : un agent sans
  // restriction n'a rien à se voir imposer, et matérialiser la clé le
  // restreindrait à la seule liste qu'on vient d'y écrire — l'inverse du but.
  const outilsRestreints = Object.prototype.hasOwnProperty.call(
    resourceEditor.frontmatter,
    "tools",
  );

  // Cocher une skill inscrit ses outils dans l'agent : c'est le seul endroit où
  // la déclaration de la skill prend effet, puisque l'agent reste seul maître
  // de ce qu'il peut appeler.
  const outilsImposes = outilsRestreints
    ? toolsRequiredBySkills(
      catalog?.skills || [],
      resourceList(resourceEditor.frontmatter.skills),
      (catalog?.tools || []).map((tool) => tool.name),
    )
    : [];

  function basculerSkill(nom: string) {
    const active = !resourceList(resourceEditor.frontmatter.skills).includes(nom);
    toggleEditorListField("skills", nom);
    if (!active || !outilsRestreints) return;
    const outils = (catalog?.skills || []).find((skill) => skill.name === nom)?.tools || [];
    // On ajoute à l'activation, on ne retire jamais : un outil peut avoir été
    // voulu pour lui-même, et le décocher au passage serait une décision qu'on
    // prend à la place de l'utilisateur.
    if (outils.length) setEditorListValues("tools", outils, true);
  }

  return (
    <div className="resource-editor">
      <div className="resource-fields">
        <label>
          Identifiant
          <input
            value={resourceEditor.id}
            disabled={!resourceEditor.creating}
            onChange={(event) => updateResourceId(event.target.value)}
            onBlur={(event) => updateResourceId(event.target.value, true)}
          />
          {resourceEditor.creating && (
            <small>Minuscules, chiffres, points, tirets ou underscores.</small>
          )}
        </label>
        <label className="field-wide">
          Description
          <input
            value={String(resourceEditor.frontmatter.description || "")}
            onChange={(event) => updateEditorField("description", event.target.value)}
          />
        </label>
        {resourceEditor.kind === "agents" && !resourceEditor.creating && (
          <div className="avatar-field field-wide">
            <AgentAvatar
              agentId={resourceEditor.id}
              label={resourceEditor.id}
              hasAvatar={agentHasAvatar(resourceEditor.id)}
              version={avatarVersion}
              className="avatar-preview"
            />
            <div>
              <strong>Avatar</strong>
              <small>PNG, JPEG, WebP ou GIF, 1 Mo maximum. Affiché dans le fil.</small>
              <div className="avatar-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={avatarBusy}
                  onClick={() => avatarInput.current?.click()}
                >
                  {avatarBusy ? "Envoi…" : "Choisir une image"}
                </button>
                {agentHasAvatar(resourceEditor.id) && (
                  <button
                    type="button"
                    className="btn danger"
                    disabled={avatarBusy}
                    onClick={() => void removeAgentAvatar(resourceEditor.id)}
                  >
                    Retirer
                  </button>
                )}
              </div>
            </div>
            <input
              ref={avatarInput}
              className="composer-file-input"
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) void uploadAgentAvatar(resourceEditor.id, file);
              }}
            />
          </div>
        )}
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
            <fieldset className="field-wide checkbox-field telegram-agent-field">
              <legend>Passerelle Telegram</legend>
              <label className="provider-vision">
                <input
                  type="checkbox"
                  checked={Boolean(resourceEditor.telegram?.enabled)}
                  onChange={(event) => updateTelegramField("enabled", event.target.checked)}
                />
                Telegram
              </label>
              {resourceEditor.telegram?.enabled && (
                <div className="resource-fields telegram-agent-settings">
                  <label className="field-wide">
                    Identifiant utilisateur Telegram
                    <input
                      type="password"
                      inputMode="numeric"
                      value={resourceEditor.telegram.user_id || ""}
                      placeholder={resourceEditor.telegram.user_id_configured
                        ? "Identifiant configuré — laisser vide pour le conserver"
                        : "Identifiant numérique autorisé"}
                      onChange={(event) => updateTelegramField("user_id", event.target.value)}
                    />
                    <small>Seul cet utilisateur pourra parler au bot, même dans un groupe.</small>
                  </label>
                  <label className="field-wide">
                    Token API du bot
                    <input
                      type="password"
                      value={resourceEditor.telegram.bot_token || ""}
                      placeholder={resourceEditor.telegram.bot_token_configured
                        ? "Token configuré — laisser vide pour le conserver"
                        : "Token fourni par BotFather"}
                      onChange={(event) => updateTelegramField("bot_token", event.target.value)}
                    />
                  </label>
                  <label className="provider-vision field-wide">
                    <input
                      type="checkbox"
                      checked={Boolean(resourceEditor.telegram.hide_session)}
                      onChange={(event) => updateTelegramField("hide_session", event.target.checked)}
                    />
                    Masquer cette session dans l’application
                  </label>
                </div>
              )}
            </fieldset>
            <fieldset className="field-wide checkbox-field">
              <legend>Autorisations par défaut</legend>
              <select
                value={String(resourceEditor.frontmatter.security_mode || "limited")}
                onChange={(event) =>
                  updateEditorField("security_mode", event.target.value)}
              >
                <option value="safe">safe — demande avant toute écriture</option>
                <option value="limited">limited — demande avant d’écraser</option>
                <option value="power">power — ne demande que le destructif</option>
              </select>
              <small>
                Niveau appliqué par les surfaces qui n’offrent pas de choix,
                Telegram et les routines. Le composer part de ce niveau et
                peut le changer message par message.
              </small>
            </fieldset>
            <fieldset className="field-wide checkbox-field">
              <legend>Niveau</legend>
              <label className="provider-vision field-wide">
                <input
                  type="checkbox"
                  checked={Boolean(resourceEditor.frontmatter.subagent)}
                  onChange={(event) => {
                    updateEditorField("subagent", event.target.checked);
                    // Un sous-agent ne délègue à personne : garder une
                    // liste d'enfants la rendrait invalide au chargement.
                    if (event.target.checked) updateEditorField("delegates", []);
                  }}
                />
                Sous-agent
              </label>
              <small>
                Un sous-agent est appelé par un orchestrateur et ne délègue
                à personne. Il n’apparaît pas comme agent principal et
                partage la bibliothèque de connaissance de celui qui
                l’invoque.
              </small>
            </fieldset>
            <fieldset className="field-wide checkbox-field">
              <legend>Mémoire personnelle</legend>
              <label className="provider-vision field-wide">
                <input
                  type="checkbox"
                  checked={Boolean(resourceEditor.frontmatter.user_memory)}
                  onChange={(event) => updateEditorField(
                    "user_memory",
                    event.target.checked,
                  )}
                />
                User memory
              </label>
              <small>
                Charge USER.md et DECISIONS.md à chaque run. Les fichiers sont créés
                dans le workspace personnel de l’agent lors de l’enregistrement.
              </small>
            </fieldset>
            <fieldset className="field-wide checkbox-field">
              <legend>Tools actifs</legend>
              <ToolSelector
                tools={catalog?.tools || []}
                modules={catalog?.modules || []}
                // Absence de clé `tools` = tout est actif. Il faut donc
                // matérialiser ce défaut pour que les cases le reflètent.
                selected={
                  Object.prototype.hasOwnProperty.call(resourceEditor.frontmatter, "tools")
                    ? resourceList(resourceEditor.frontmatter.tools)
                    : catalog?.tools.map((item) => item.name) || []
                }
                locked={outilsImposes}
                onToggleTools={(names, next) => setEditorListValues(
                  "tools",
                  names,
                  next,
                  catalog?.tools.map((item) => item.name) || [],
                )}
              />
            </fieldset>
            <fieldset className="field-wide checkbox-field">
              <legend>Skills préchargées</legend>
              <div className="checkbox-grid">
                {catalog?.skills.map((skill) => (
                  <label key={skill.name} title={skill.description}>
                    <input
                      type="checkbox"
                      checked={resourceList(resourceEditor.frontmatter.skills).includes(skill.name)}
                      onChange={() => basculerSkill(skill.name)}
                    />
                    <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
                  </label>
                ))}
                {catalog?.skills.length === 0 && <p>Aucune skill installée.</p>}
              </div>
            </fieldset>
            {/* Un sous-agent ne délègue à personne : lui proposer des
                enfants suggérerait une hiérarchie qui n'existe pas. */}
            {!resourceEditor.frontmatter.subagent && (
              <fieldset className="field-wide checkbox-field">
                <legend>Sous-agents accessibles</legend>
                <p className="field-note">
                  Sans sélection, cet orchestrateur accède à tous les
                  sous-agents. En cocher revient à restreindre — un
                  sous-agent ajouté plus tard ne lui serait alors pas
                  proposé.
                </p>
                <div className="checkbox-field-actions">
                  <button type="button" onClick={() => updateEditorField("delegates", [])}>
                    Tous
                  </button>
                </div>
                <div className="checkbox-grid">
                  {catalog?.agents
                    .filter((agent) => agent.subagent)
                    .map((agent) => (
                      <label key={agent.id} title={agent.description}>
                        <input
                          type="checkbox"
                          checked={resourceList(resourceEditor.frontmatter.delegates).includes(agent.id)}
                          onChange={() => toggleEditorListField("delegates", agent.id)}
                        />
                        <span><strong>{agent.id}</strong><small>{agent.description}</small></span>
                      </label>
                    ))}
                  {!catalog?.agents.some((agent) => agent.subagent) && (
                    <p>Aucun sous-agent défini.</p>
                  )}
                </div>
              </fieldset>
            )}
          </>
        ) : (
          <fieldset className="field-wide checkbox-field">
            <legend>Outils autorisés</legend>
            <ToolSelector
              tools={catalog?.tools || []}
              modules={catalog?.modules || []}
              selected={resourceList(resourceEditor.frontmatter["allowed-tools"])}
              onToggleTools={(names, next) =>
                setEditorListValues("allowed-tools", names, next)}
            />
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
          onClick={() => onSave()}
        >
          {savingResource ? "Enregistrement…" : "Enregistrer"}
        </button>
      </div>
    </div>
  );
}
