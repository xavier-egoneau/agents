"use client";

import { useMemo, useState } from "react";

import { Icon } from "../theme/theme-context";

/**
 * Sélection d'outils par module.
 *
 * Le kernel expose 74 tools répartis en 15 modules. Les lister à plat demandait
 * de lire `browser_open`, `browser_snapshot`, `browser_click`… pour comprendre
 * qu'on parlait d'une seule capacité : naviguer sur le web. Le module porte ce
 * nom-là, et c'est à ce niveau que la décision se prend.
 *
 * Le détail reste accessible par dépliage : couper un seul outil d'un module —
 * l'écriture de fichiers, par exemple — est un besoin réel et le frontmatter
 * sait l'exprimer, puisqu'il nomme les outils un par un.
 */
export type ToolEntry = { name: string; description: string; module: string };
export type ModuleEntry = { id: string; name: string; description: string };

type ToolSelectorProps = {
  tools: ToolEntry[];
  modules: ModuleEntry[];
  selected: string[];
  onToggleTools: (names: string[], next: boolean) => void;
};

export function ToolSelector({ tools, modules, selected, onToggleTools }: ToolSelectorProps) {
  const [expanded, setExpanded] = useState<string[]>([]);
  const chosen = useMemo(() => new Set(selected), [selected]);

  const groups = useMemo(() => {
    const byModule = new Map<string, ToolEntry[]>();
    for (const tool of tools) {
      const bucket = byModule.get(tool.module);
      if (bucket) bucket.push(tool);
      else byModule.set(tool.module, [tool]);
    }
    // Un module sans nom déclaré reste affiché sous son identifiant : le taire
    // masquerait des outils réellement disponibles.
    const labels = new Map(modules.map((module) => [module.id, module]));
    return [...byModule.entries()]
      .map(([id, entries]) => ({
        id,
        label: labels.get(id)?.name || id,
        description: labels.get(id)?.description || "",
        tools: entries,
      }))
      .sort((left, right) => left.label.localeCompare(right.label, "fr"));
  }, [tools, modules]);

  return (
    <div className="tool-selector">
      {groups.map((group) => {
        const active = group.tools.filter((tool) => chosen.has(tool.name)).length;
        const all = active === group.tools.length;
        const partial = active > 0 && !all;
        const open = expanded.includes(group.id);
        const names = group.tools.map((tool) => tool.name);

        return (
          <div className="tool-group" key={group.id}>
            <div className="tool-group-head">
              <label title={group.description}>
                <input
                  type="checkbox"
                  checked={all}
                  // Un module à moitié actif n'est ni coché ni décoché : sans cet
                  // état, l'un des deux mentirait sur ce qui est réellement chargé.
                  ref={(node) => {
                    if (node) node.indeterminate = partial;
                  }}
                  onChange={() => onToggleTools(names, !all)}
                />
                <span>
                  <strong>{group.label}</strong>
                  <small>
                    {active === group.tools.length
                      ? `${group.tools.length} outil${group.tools.length > 1 ? "s" : ""}`
                      : `${active} sur ${group.tools.length}`}
                  </small>
                </span>
              </label>
              <button
                type="button"
                className="tool-group-toggle"
                aria-expanded={open}
                aria-label={`${open ? "Masquer" : "Afficher"} le détail de ${group.label}`}
                onClick={() =>
                  setExpanded((current) =>
                    current.includes(group.id)
                      ? current.filter((id) => id !== group.id)
                      : [...current, group.id],
                  )
                }
              >
                <Icon name={open ? "chevronDown" : "chevronRight"} size="sm" />
              </button>
            </div>
            {open && (
              <div className="tool-group-detail">
                {group.tools.map((tool) => (
                  <label key={tool.name} title={tool.description}>
                    <input
                      type="checkbox"
                      checked={chosen.has(tool.name)}
                      onChange={() => onToggleTools([tool.name], !chosen.has(tool.name))}
                    />
                    <span>{tool.name}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
