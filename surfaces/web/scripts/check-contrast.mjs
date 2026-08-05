#!/usr/bin/env node
/**
 * Vérificateur de contraste des thèmes.
 *
 * Objectif : garantir qu'aucun futur template ne puisse casser la lisibilité
 * sans qu'on s'en aperçoive. Le script résout les variables de chaque thème
 * (en retombant sur tokens.css pour celles qu'il ne redéfinit pas), reproduit
 * les dérivations `color-mix` des tokens de contexte, puis calcule le ratio
 * WCAG 2.1 de chaque paire critique — y compris les états au survol, qui sont
 * la source d'erreur la plus fréquente.
 *
 * Usage : node scripts/check-contrast.mjs
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const THEME_DIR = join(ROOT, "app", "theme");

/* --- Lecture des déclarations ------------------------------------------- */

/** Extrait les paires `--nom: valeur;` d'une feuille de style. */
function parseDeclarations(css) {
  const declarations = new Map();
  const pattern = /(--[\w-]+)\s*:\s*([^;]+);/g;
  let match;
  while ((match = pattern.exec(css)) !== null) {
    declarations.set(match[1], match[2].trim());
  }
  return declarations;
}

/* --- Couleurs ------------------------------------------------------------- */

function parseHex(value) {
  let hex = value.trim().replace(/^#/, "");
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  if (hex.length === 8) hex = hex.slice(0, 6); // alpha ignoré : on compare au fond opaque
  if (hex.length !== 6 || !/^[0-9a-f]{6}$/i.test(hex)) return null;
  return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

/** Interpolation sRGB, équivalent de `color-mix(in srgb, a p%, b)`. */
function mix(a, b, ratio) {
  return a.map((channel, i) => Math.round(channel * ratio + b[i] * (1 - ratio)));
}

function relativeLuminance([r, g, b]) {
  const channel = (value) => {
    const v = value / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrastRatio(fg, bg) {
  const a = relativeLuminance(fg);
  const b = relativeLuminance(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/* --- Résolution des variables --------------------------------------------- */

/**
 * Résout une valeur de token en couleur RGB. Gère `var()`, les couleurs
 * littérales et `color-mix(in srgb, X p%, Y)` — la seule forme utilisée par le
 * contrat de tokens.
 */
function resolve(name, scope, seen = new Set()) {
  if (seen.has(name)) return null; // cycle
  seen.add(name);

  const raw = scope.get(name);
  if (!raw) return null;
  return resolveValue(raw, scope, seen);
}

function resolveValue(raw, scope, seen) {
  const value = raw.trim();

  if (value.startsWith("#")) return parseHex(value);
  if (value === "white") return [255, 255, 255];
  if (value === "black") return [0, 0, 0];

  const varMatch = value.match(/^var\(\s*(--[\w-]+)\s*(?:,([\s\S]+))?\)$/);
  if (varMatch) {
    const resolved = resolve(varMatch[1], scope, seen);
    if (resolved) return resolved;
    return varMatch[2] ? resolveValue(varMatch[2], scope, seen) : null;
  }

  const mixMatch = value.match(
    /^color-mix\(\s*in\s+srgb\s*,\s*([\s\S]+?)\s+([\d.]+)%\s*,\s*([\s\S]+?)\s*\)$/,
  );
  if (mixMatch) {
    const first = resolveValue(mixMatch[1], scope, new Set(seen));
    const second = resolveValue(mixMatch[3], scope, new Set(seen));
    if (!first || !second) return null;
    return mix(first, second, Number(mixMatch[2]) / 100);
  }

  return null;
}

/* --- Paires vérifiées ------------------------------------------------------ */

/**
 * `min` suit WCAG 2.1 : 4.5 pour du texte courant, 3 pour du texte large ou
 * des éléments d'interface (bordures, icônes), 2 pour des séparateurs dont la
 * seule fonction est décorative.
 */
const PAIRS = [
  // Contexte clair (surfaces applicatives)
  ["--text-default", "--surface-app", 4.5, "texte courant sur le fond de page"],
  ["--text-default", "--surface-panel", 4.5, "texte courant sur panneau"],
  ["--text-strong", "--surface-panel", 4.5, "titres sur panneau"],
  ["--text-muted", "--surface-panel", 4.5, "texte secondaire sur panneau"],
  ["--text-faint", "--surface-panel", 3, "texte tertiaire sur panneau"],
  ["--text-link", "--surface-panel", 4.5, "liens"],
  // Séparateur décoratif : aucune exigence WCAG, on veut juste qu'il se voie.
  ["--border-default", "--surface-panel", 1.2, "séparateurs sur panneau"],
  // Contour de champ de saisie : composant d'interface, 3:1 requis.
  ["--control-border", "--surface-panel", 3, "contour des champs de saisie"],

  // Accent et états
  ["--text-on-accent", "--accent", 4.5, "texte sur bouton accent"],
  ["--text-on-accent", "--accent-hover", 4.5, "texte sur bouton accent survolé"],
  ["--accent", "--surface-panel", 3, "accent sur panneau"],
  ["--accent-soft-text", "--accent-soft", 4.5, "texte sur pastille accent"],
  ["--danger", "--surface-panel", 3, "erreur sur panneau"],
  ["--danger-text", "--danger-soft", 4.5, "texte sur bandeau erreur"],
  ["--warning-text", "--warning-soft", 4.5, "texte sur bandeau avertissement"],
  ["--success-text", "--success-soft", 4.5, "texte sur bandeau succès"],
  ["--text-on-accent", "--danger", 4.5, "texte sur action destructive"],

  // Chrome sombre : rail
  ["--text-on-rail", "--surface-rail", 4.5, "texte sur le rail"],
  ["--text-on-rail", "--surface-rail-alt", 4.5, "texte sur le panneau latéral"],
  ["--text-on-rail-muted", "--surface-rail", 4.5, "texte secondaire sur le rail"],
  ["--text-on-rail", "--surface-rail-hover", 4.5, "texte sur le rail survolé"],
  ["--text-on-rail", "--surface-rail-active", 4.5, "texte sur le rail actif"],
  ["--accent-on-rail", "--surface-rail", 3, "accent sur le rail"],
  ["--accent-on-rail", "--surface-rail-alt", 3, "accent sur le panneau latéral"],

  // Chrome sombre : dock
  ["--text-on-dock", "--surface-dock", 4.5, "texte sur le dock"],
  // Diff : la teinte porte l'information (ajout / suppression), elle doit donc
  // rester lisible et pas seulement décorative.
  ["--diff-added-on-dock", "--surface-dock", 4.5, "lignes ajoutées"],
  ["--diff-removed-on-dock", "--surface-dock", 4.5, "lignes supprimées"],
  ["--diff-hunk-on-dock", "--surface-dock", 4.5, "en-tête de section du patch"],
  ["--diff-added", "--surface-panel-alt", 4.5, "compteur d'ajouts sur la carte"],
  ["--diff-removed", "--surface-panel-alt", 4.5, "compteur de retraits sur la carte"],
  ["--text-on-dock-muted", "--surface-dock", 4.5, "texte secondaire sur le dock"],
  ["--text-on-dock", "--surface-dock-alt", 4.5, "texte sur la barre d'onglets"],
  ["--code-fg", "--code-bg", 4.5, "code"],
  ["--inline-code-fg", "--inline-code-bg", 4.5, "code en ligne"],
];

/**
 * États dérivés des tokens de contexte. Pour chaque scope on rejoue la
 * dérivation `color-mix` et on vérifie que le texte reste lisible sur le fond
 * survolé — c'est exactement le cas qui produisait du blanc sur blanc.
 */
const CONTEXTS = [
  ["panneau clair", "--surface-panel", "--text-default", "--accent"],
  ["fond de page", "--surface-app", "--text-default", "--accent"],
  ["rail", "--surface-rail", "--text-on-rail", "--accent-on-rail"],
  ["panneau latéral", "--surface-rail-alt", "--text-on-rail", "--accent-on-rail"],
  ["dock", "--surface-dock", "--text-on-dock", "--accent-on-rail"],
  ["onglets du dock", "--surface-dock-alt", "--text-on-dock", "--accent-on-rail"],
  ["panneau alterné", "--surface-panel-alt", "--text-default", "--accent"],
  ["modale", "--surface-overlay", "--text-default", "--accent"],
];

const DERIVED = [
  ["--ctx-text-muted", "--ctx-hover", 4.5, "texte secondaire au survol"],
  ["--ctx-text", "--ctx-hover", 4.5, "texte au survol"],
  ["--ctx-text", "--ctx-active", 4.5, "texte sur élément actif"],
  ["--ctx-text-faint", "--ctx-surface", 3, "texte tertiaire"],
  ["--ctx-accent", "--ctx-surface", 3, "accent"],
  ["--ctx-border", "--ctx-surface", 1.2, "séparateur"],
  ["--ctx-border-control", "--ctx-surface", 3, "contour de champ"],
  // Pastille de skill préchargée : le creusé assombrit le fond sous un texte
  // déjà atténué, cumul que les paires ci-dessus ne couvrent pas.
  ["--ctx-text-muted", "--ctx-sunken", 4.5, "pastille de skill préchargée"],
  ["--ctx-border", "--ctx-sunken", 1.2, "contour de la pastille"],
];

/* --- Exécution ------------------------------------------------------------- */

/**
 * Contrôle structurel : toute règle qui déclare `--ctx-surface` doit aussi
 * redéclarer les dérivations. Une custom property hérite sa valeur *calculée*,
 * donc un scope qui ne redéclare pas `--ctx-hover` hérite celui de `:root`,
 * calculé sur la surface claire — d'où du texte blanc sur fond blanc dans le
 * rail sombre. Ce test empêche la régression de revenir.
 */
function checkScopeCompleteness() {
  const css = readdirSync(THEME_DIR)
    .filter((f) => f.endsWith(".css"))
    .map((f) => readFileSync(join(THEME_DIR, f), "utf8"))
    .join("\n")
    // Les commentaires contiennent des accolades et du texte libre : sans
    // les retirer, le découpage en règles produit de faux sélecteurs.
    .replace(/\/\*[\s\S]*?\*\//g, "");

  const selectorsOf = (property) => {
    const found = new Set();
    const rules = css.matchAll(/([^{}]+)\{([^{}]*)\}/g);
    for (const [, selector, body] of rules) {
      if (!body.includes(`${property}:`)) continue;
      selector
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s && !s.startsWith("@") && !s.startsWith("/*"))
        .forEach((s) => found.add(s));
    }
    return found;
  };

  const scopes = selectorsOf("--ctx-surface");
  const derived = selectorsOf("--ctx-hover");
  const missing = [...scopes].filter((s) => !derived.has(s));

  if (missing.length) {
    console.error("\nScopes de contexte incomplets — ils déclarent --ctx-surface");
    console.error("sans redéclarer les dérivations, donc leur survol viendra de :root :");
    missing.forEach((s) => console.error(`  ✗ ${s}`));
    console.error("\nAjoutez-les au bloc de dérivation dans base.css.\n");
    return missing.length;
  }
  console.log(`Structure des scopes — OK (${scopes.size} scopes complets)`);
  return 0;
}

const structural = checkScopeCompleteness();

const base = parseDeclarations(readFileSync(join(THEME_DIR, "tokens.css"), "utf8"));

// Les formules de dérivation vivent dans base.css (elles doivent être répétées
// par scope). On les récupère ici, en ignorant les valeurs d'entrée que chaque
// scope redéfinit — celles-ci sont injectées par la boucle CONTEXTS.
const INPUTS = new Set(["--ctx-surface", "--ctx-text", "--ctx-accent"]);
for (const [name, value] of parseDeclarations(readFileSync(join(THEME_DIR, "base.css"), "utf8"))) {
  if (name.startsWith("--ctx-") && !INPUTS.has(name)) base.set(name, value);
}
const themeFiles = readdirSync(join(THEME_DIR, "themes")).filter((f) => f.endsWith(".css"));

let failures = 0;
let checks = 0;

for (const file of themeFiles) {
  const themeId = file.replace(/\.css$/, "");
  const overrides = parseDeclarations(readFileSync(join(THEME_DIR, "themes", file), "utf8"));
  const scope = new Map([...base, ...overrides]);

  const problems = [];

  const check = (fgName, bgName, min, label, prefix = "") => {
    const fg = resolve(fgName, scope);
    const bg = resolve(bgName, scope);
    if (!fg || !bg) {
      problems.push(`  ? ${prefix}${label} — token non résolu (${fgName} / ${bgName})`);
      return;
    }
    checks += 1;
    const ratio = contrastRatio(fg, bg);
    if (ratio < min) {
      problems.push(
        `  ✗ ${prefix}${label} — ${ratio.toFixed(2)}:1 (minimum ${min}:1)\n` +
        `      ${fgName} sur ${bgName}`,
      );
    }
  };

  for (const [fg, bg, min, label] of PAIRS) check(fg, bg, min, label);

  for (const [ctxLabel, surfaceToken, textToken, accentToken] of CONTEXTS) {
    // Rejoue la déclaration d'un scope : deux valeurs, le reste est dérivé.
    const ctxScope = new Map(scope);
    ctxScope.set("--ctx-surface", `var(${surfaceToken})`);
    ctxScope.set("--ctx-text", `var(${textToken})`);
    ctxScope.set("--ctx-accent", `var(${accentToken})`);

    for (const [fg, bg, min, label] of DERIVED) {
      const fgColor = resolve(fg, ctxScope);
      const bgColor = resolve(bg, ctxScope);
      if (!fgColor || !bgColor) {
        problems.push(`  ? [${ctxLabel}] ${label} — token non résolu`);
        continue;
      }
      checks += 1;
      const ratio = contrastRatio(fgColor, bgColor);
      if (ratio < min) {
        problems.push(
          `  ✗ [${ctxLabel}] ${label} — ${ratio.toFixed(2)}:1 (minimum ${min}:1)`,
        );
      }
    }
  }

  if (problems.length) {
    failures += problems.length;
    console.log(`\n${themeId} — ${problems.length} problème(s)`);
    problems.forEach((line) => console.log(line));
  } else {
    console.log(`${themeId} — OK`);
  }
}

console.log(`\n${checks} paires vérifiées sur ${themeFiles.length} thèmes.`);

if (failures || structural) {
  if (failures) console.error(`\n${failures} contraste(s) insuffisant(s).`);
  if (structural) console.error(`${structural} scope(s) de contexte incomplet(s).`);
  process.exit(1);
}
