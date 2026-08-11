"use client";

/**
 * PARAMÈTRES DES MODULES
 * -----------------------------------------------------------------------------
 * Rend les champs déclarés par chaque module outillé, et laisse l'appelant
 * décider quoi faire de la saisie. Aucun état propre : `page.tsx` garde la
 * source de vérité, ce composant ne fait que l'afficher.
 *
 * `header` existe parce que la modale est une grille : y insérer un fragment
 * plaçait deux enfants sur la même ligne, côte à côte. Passer le contenu en
 * propriété le garde dans le flux vertical.
 */

import type { ConfigurableModule } from "../lib/model";

export function ModuleSettingsPanel({
  modules,
  saving,
  onChange,
  onSave,
  header,
}: {
  modules: ConfigurableModule[];
  saving: boolean;
  onChange: (moduleId: string, fieldName: string, value: unknown) => void;
  onSave: (module: ConfigurableModule) => void;
  /** Rendu au-dessus des modules, dans le même corps défilant. La modale place
   *  tous ses enfants directs dans une seule cellule de grille : un second bloc
   *  frère s'y serait rangé en colonne, à côté de celui-ci. */
  header?: React.ReactNode;
}) {
  const stateLabel = {
    configured: "Configuré",
    defaults: "Prêt avec les valeurs par défaut",
    required: "Configuration requise",
  };
  return (
    <div className="management-body module-settings-list">
      {header}
      {modules.map((module) => (
        <section className="module-settings-card" key={module.id}>
          <header>
            <div>
              <strong>{module.title}</strong>
              <small>{module.description}</small>
            </div>
            <span className="module-settings-state" data-state={module.state}>
              {stateLabel[module.state]}
            </span>
          </header>
          {module.applies_to.length > 0 && (
            <p className="module-settings-tools">
              Tools : {module.applies_to.join(", ")}
            </p>
          )}
          <div className="resource-fields">
            {module.fields.map((field) => {
              const fieldId = `${module.id}-${field.name}`;
              const description = field.description && <small>{field.description}</small>;
              if (field.type === "boolean") {
                return (
                  <label className="provider-vision" key={field.name} htmlFor={fieldId}>
                    <input
                      id={fieldId}
                      type="checkbox"
                      checked={Boolean(field.value)}
                      onChange={(event) => onChange(module.id, field.name, event.target.checked)}
                    />
                    {field.label}
                    {description}
                  </label>
                );
              }
              if (field.type === "select") {
                return (
                  <label key={field.name} htmlFor={fieldId}>
                    {field.label}{field.required && " *"}
                    <select
                      id={fieldId}
                      value={String(field.value ?? "")}
                      onChange={(event) => onChange(module.id, field.name, event.target.value)}
                    >
                      {!field.required && <option value="">Valeur par défaut</option>}
                      {field.options.map((option) => <option key={option}>{option}</option>)}
                    </select>
                    {description}
                  </label>
                );
              }
              if (field.type === "string_list") {
                return (
                  <label className="field-wide" key={field.name} htmlFor={fieldId}>
                    {field.label}{field.required && " *"}
                    <textarea
                      id={fieldId}
                      className="module-settings-list-input"
                      value={Array.isArray(field.value) ? field.value.join("\n") : ""}
                      onChange={(event) => onChange(
                        module.id,
                        field.name,
                        event.target.value.split(/\r?\n/).filter(Boolean),
                      )}
                    />
                    {description}
                  </label>
                );
              }
              const numeric = field.type === "integer" || field.type === "number";
              return (
                <label
                  className={field.type === "secret" || field.type === "file" || field.type === "directory" ? "field-wide" : ""}
                  key={field.name}
                  htmlFor={fieldId}
                >
                  {field.label}{field.required && " *"}
                  <input
                    id={fieldId}
                    type={field.type === "secret" ? "password" : numeric ? "number" : "text"}
                    min={field.minimum ?? undefined}
                    max={field.maximum ?? undefined}
                    step={field.type === "number" ? "any" : undefined}
                    value={String(field.value ?? "")}
                    placeholder={field.type === "secret" && field.configured
                      ? "Valeur configurée — laisser vide pour la conserver"
                      : field.type === "file" || field.type === "directory"
                        ? "Chemin absolu"
                        : undefined}
                    onChange={(event) => onChange(
                      module.id,
                      field.name,
                      numeric && event.target.value !== ""
                        ? Number(event.target.value)
                        : event.target.value,
                    )}
                  />
                  {description}
                </label>
              );
            })}
          </div>
          <footer>
            <button
              className="primary"
              disabled={saving}
              onClick={() => onSave(module)}
            >
              {saving ? "Enregistrement…" : "Enregistrer"}
            </button>
          </footer>
        </section>
      ))}
      {modules.length === 0 && (
        <div className="management-empty">
          <strong>Aucun module configurable</strong>
          <p>Les tools sans paramètres restent disponibles dans le catalogue.</p>
        </div>
      )}
    </div>
  );
}
