/**
 * REGISTRY DE THEMES
 * -----------------------------------------------------------------------------
 * Source de verite unique de la surcouche : un theme = une palette CSS
 * (app/theme/themes/<id>.css) + d'eventuelles surcharges d'icones
 * (app/theme/icons.ts) + les metadonnees ci-dessous.
 *
 * Pour ajouter un theme :
 *   1. creer app/theme/themes/<id>.css avec `[data-theme="<id>"] { ... }`
 *   2. l'importer dans app/globals.css
 *   3. ajouter son entree ici
 * Aucun autre fichier n'a besoin d'etre touche.
 */

export type ThemeId = "graphite" | "paper" | "midnight" | "editorial";

export type ThemeDefinition = {
  id: ThemeId;
  label: string;
  description: string;
  /** Aperçu pour le selecteur : [fond, surface, accent]. */
  swatch: [string, string, string];
  scheme: "light" | "dark";
};

export const THEMES: ThemeDefinition[] = [
  {
    id: "graphite",
    label: "Graphite",
    description: "Clair et neutre, accent teal",
    swatch: ["#f6f7f8", "#16181b", "#0d8074"],
    scheme: "light",
  },
  {
    id: "paper",
    label: "Papier",
    description: "Palette chaude d'origine",
    swatch: ["#f4f1e9", "#20262a", "#006d77"],
    scheme: "light",
  },
  {
    id: "midnight",
    label: "Midnight",
    description: "Sombre integral, accent indigo",
    swatch: ["#0f1115", "#0b0d11", "#818cf8"],
    scheme: "dark",
  },
  {
    id: "editorial",
    label: "Editorial",
    description: "Serif, angles vifs, accent brique",
    swatch: ["#fbfbf9", "#1a1917", "#9a3412"],
    scheme: "light",
  },
];

export const DEFAULT_THEME: ThemeId = "graphite";
export const THEME_STORAGE_KEY = "amk.theme";

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === "string" && THEMES.some((theme) => theme.id === value);
}

/**
 * Script inline injecte avant l'hydratation : applique le theme memorise
 * sans flash de couleur incorrecte.
 */
export const THEME_BOOTSTRAP_SCRIPT = `(function(){try{var t=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});var a=${JSON.stringify(
  THEMES.map((theme) => theme.id),
)};document.documentElement.dataset.theme=a.indexOf(t)>-1?t:${JSON.stringify(
  DEFAULT_THEME,
)};}catch(e){document.documentElement.dataset.theme=${JSON.stringify(
  DEFAULT_THEME,
)};}})();`;
