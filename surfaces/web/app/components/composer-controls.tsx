"use client";

import { Icon } from "../theme/theme-context";

export type SecurityMode = "safe" | "limited" | "power";
/** `off` par défaut : la bibliothèque n'entre dans le contexte que si on le demande. */
export type KnowledgeMode = "off" | "auto" | "manual";
export type ReasoningLevel = "minimal" | "low" | "medium" | "high" | "xhigh";

type ProviderModels = {
  id: string;
  models: string[];
};

type ComposerControlsProps = {
  providers: ProviderModels[];
  providerId: string;
  model: string;
  securityMode: SecurityMode;
  reasoning: ReasoningLevel;
  running: boolean;
  stopRequested: boolean;
  canAttach: boolean;
  canSend: boolean;
  vision: boolean;
  knowledgeMode: KnowledgeMode;
  /** Nombre de pages retenues, affiché en pastille en mode manuel. */
  knowledgeCount: number;
  onKnowledgeModeChange: (mode: KnowledgeMode) => void;
  onKnowledgeSelect: () => void;
  onAttach: () => void;
  onSecurityModeChange: (mode: SecurityMode) => void;
  onModelChange: (providerId: string, model: string) => void;
  onReasoningChange: (reasoning: ReasoningLevel) => void;
  onStop: () => void;
};

export function ComposerControls({
  providers,
  providerId,
  model,
  securityMode,
  reasoning,
  running,
  stopRequested,
  canAttach,
  canSend,
  vision,
  knowledgeMode,
  knowledgeCount,
  onKnowledgeModeChange,
  onKnowledgeSelect,
  onAttach,
  onSecurityModeChange,
  onModelChange,
  onReasoningChange,
  onStop,
}: ComposerControlsProps) {
  return (
    <div className="composer-footer">
      <div className="composer-controls">
        <button
          type="button"
          className="composer-icon-button"
          onClick={onAttach}
          disabled={!canAttach}
          aria-label="Ajouter une image"
          title={vision ? "Ajouter une image" : "Ajouter une image · analyse locale Gemma 4"}
        ><Icon name="attach" size="md" /></button>
        {/* Deux gestes distincts sur un même contrôle : l'icône ouvre la
            sélection, le menu change de mode. Les séparer évitait un bouton
            qui fait deux choses selon l'endroit exact du clic. */}
        <div className="knowledge-control" data-active={knowledgeMode !== "off" ? "true" : undefined}>
          <button
            type="button"
            className="composer-icon-button"
            onClick={onKnowledgeSelect}
            disabled={knowledgeMode !== "manual"}
            aria-label="Choisir les pages de connaissance"
            title={
              knowledgeMode === "manual"
                ? `Choisir les pages · ${knowledgeCount} retenue(s)`
                : "Bibliothèque de connaissance"
            }
          >
            <Icon name="knowledge" size="md" />
            {knowledgeMode === "manual" && knowledgeCount > 0 && (
              <span className="knowledge-count">{knowledgeCount}</span>
            )}
          </button>
          <select
            value={knowledgeMode}
            onChange={(event) => onKnowledgeModeChange(event.target.value as KnowledgeMode)}
            aria-label="Mode de connaissance"
            title="Bibliothèque de connaissance"
          >
            <option value="off">sans</option>
            <option value="auto">auto</option>
            <option value="manual">manuel</option>
          </select>
        </div>
        <label title="Niveau de permission">
          <span className={`permission-dot ${securityMode}`} />
          <select
            value={securityMode}
            onChange={(event) => onSecurityModeChange(event.target.value as SecurityMode)}
            aria-label="Niveau de permission"
          >
            <option value="safe">safe</option>
            <option value="limited">limited</option>
            <option value="power">power</option>
          </select>
        </label>
        <label className="model-control" title="Provider et modèle">
          <select
            value={`${providerId}::${model}`}
            disabled={running}
            onChange={(event) => {
              const [nextProvider, ...modelParts] = event.target.value.split("::");
              onModelChange(nextProvider, modelParts.join("::"));
            }}
            aria-label="Provider et modèle"
          >
            {providers.map((provider) => (
              <optgroup key={provider.id} label={provider.id}>
                {provider.models.map((providerModel) => (
                  <option
                    key={`${provider.id}:${providerModel}`}
                    value={`${provider.id}::${providerModel}`}
                  >
                    {providerModel}
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
            onChange={(event) => onReasoningChange(event.target.value as ReasoningLevel)}
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
            onClick={onStop}
            disabled={stopRequested}
            aria-label="Arrêter le run"
            title="Arrêter le run"
          ><span /></button>
        )}
        <button className="send" disabled={!canSend} aria-label="Envoyer">
          <Icon name="send" size="md" />
        </button>
      </div>
    </div>
  );
}
