"use client";

export type SecurityMode = "safe" | "limited" | "power";
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
        >+</button>
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
        <button className="send" disabled={!canSend} aria-label="Envoyer">↑</button>
      </div>
    </div>
  );
}
