# Système de templating

L'apparence de la surface web est découpée en deux couches strictement séparées.

## 1. Couche fixe

| Fichier | Rôle |
|---|---|
| `tokens.css` | **Le contrat.** Déclare toutes les variables qu'un thème peut redéfinir, avec des valeurs de repli neutres. |
| `base.css` | Reset, primitives (`.ibtn`, `.btn`, infobulles), shell (rail, panneaux, dock, poignées). |
| `components*.css` | Les composants métier : conversation, modale de config, plan/trace, workflow. |

**Règle unique :** aucun `#hex`, aucune police, aucun rayon en dur dans ces fichiers. Si une valeur visuelle manque, on ajoute d'abord le token dans `tokens.css`.

Les rares exceptions tolérées sont les dégradés de lisibilité posés sur des images (`.image-previews figcaption`), où la couleur ne dépend pas du thème.

## 2. Couche de templating

| Fichier | Rôle |
|---|---|
| `themes/<id>.css` | La palette. Ne contient qu'un bloc `[data-theme="<id>"] { … }` de redéfinitions de tokens. |
| `icons.ts` | Le registry sémantique : `agent`, `send`, `diff`… → composants lucide. Surchargeable par thème. |
| `registry.ts` | Métadonnées des thèmes (libellé, aperçu, schéma clair/sombre) + script anti-flash. |
| `theme-context.tsx` | `ThemeProvider`, `useTheme()` et le composant `<Icon>`. |

Le thème actif est porté par `data-theme` sur `<html>`, appliqué avant le premier paint et persisté dans `localStorage`.

## Ajouter un thème

1. Créer `themes/mon-theme.css` :

```css
[data-theme="mon-theme"] {
  color-scheme: light;
  --surface-app: #…;
  --accent: #…;
  --font-display: "…", serif;
  --radius-md: 4px;
}
```

2. L'importer dans `app/globals.css`.
3. Ajouter son entrée dans `THEMES` (`registry.ts`) et son id dans le type `ThemeId`.

Aucun composant React n'a besoin d'être touché. Le thème `editorial` sert de démonstration : il change la typographie, la géométrie et l'échelle de tailles, pas seulement les couleurs.

## Changer les icônes d'un thème

Dans `icons.ts` :

```ts
export const themeIconSets: Record<string, IconSet> = {
  "mon-theme": { agent: Rocket, send: CornerDownLeft },
};
```

Les rôles non redéfinis retombent sur `baseIcons`.

**Contrainte :** `lucide-react` ne doit être importé que dans `icons.ts`. Partout ailleurs on passe par `<Icon name="…" />`, sinon un thème ne peut plus reprendre la main sur l'iconographie.

## Contraste : garanti par construction

Le piège classique : un composant écrit pour le rail sombre (`color: var(--text-on-rail)`) est réutilisé dans une modale claire — il devient blanc sur blanc, souvent seulement au survol, donc invisible en revue.

La parade est le **contexte de surface**. Un conteneur qui change de luminosité déclare deux valeurs :

```css
.ma-surface {
  --ctx-surface: var(--surface-rail);
  --ctx-text: var(--text-on-rail);
  --ctx-accent: var(--accent-on-rail);
}
```

Tout le reste est dérivé par mélange entre ces deux couleurs :

| Token | Dérivation | Usage |
|---|---|---|
| `--ctx-text-muted` | 76 % texte | texte secondaire |
| `--ctx-text-faint` | 60 % texte | texte tertiaire |
| `--ctx-hover` | 9 % texte | fond au survol |
| `--ctx-active` | 16 % texte | fond d'élément actif |
| `--ctx-sunken` | 6 % texte | fond de champ |
| `--ctx-border` | 20 % texte | séparateur |
| `--ctx-border-control` | 55 % texte | contour de champ (3:1) |

Le hover étant toujours un mélange avec la surface *du contexte courant*, il ne peut pas devenir invisible, quel que soit le thème et qu'il soit clair ou sombre.

### Le piège de l'héritage — à lire avant d'ajouter un scope

Une custom property hérite sa valeur **calculée**. Les `var()` qu'elle contient sont donc résolus là où elle est *déclarée*, pas là où elle est *utilisée*.

Conséquence : si `--ctx-hover` n'est déclaré que dans `:root`, il est calculé une fois pour toutes avec la surface claire de `:root`. Un scope qui redéfinit `--ctx-surface` hérite quand même le hover clair — et produit du texte blanc sur fond blanc dans le rail sombre.

C'est pourquoi les dérivations vivent dans `base.css`, dans un bloc qui **liste tous les scopes**, et non dans `tokens.css`. Déclarer un nouveau contexte se fait donc en deux temps :

1. ajouter le sélecteur au bloc de dérivation (section « SCOPES DE CONTEXTE », partie 1) ;
2. déclarer ses trois valeurs d'entrée (partie 2).

Oublier l'étape 1 est détecté par `npm run check:contrast`.

**Règle :** un composant susceptible d'apparaître sur deux surfaces différentes n'utilise que des `--ctx-*`. Il n'existe volontairement pas de token `--btn-ghost-bg-hover` : un bouton discret n'a pas de couleur propre, il prend celle de la surface qui le porte.

### Vérification

```bash
npm run check:contrast
```

Deux contrôles :

- **structurel** — tout sélecteur qui déclare `--ctx-surface` redéclare-t-il bien les dérivations ? (protège du piège d'héritage ci-dessus) ;
- **colorimétrique** — 340 paires réparties sur les 4 thèmes et les 8 contextes, dont les états au survol et actif de chacun.

Seuils WCAG 2.1 : 4.5:1 pour le texte, 3:1 pour l'accent et les contours de champ, 1.2:1 pour les séparateurs décoratifs. Sortie en code 1 en cas d'échec, donc branchable en CI.

## Groupes de tokens

- **Surfaces** — `--surface-app`, `-panel`, `-panel-alt`, `-sunken`, `-overlay`, `-hover`, `-active`, `-scrim`, plus les surfaces « chrome » `-rail*` et `-dock*`.
- **Texte** — `--text-strong`, `-default`, `-muted`, `-faint`, `-on-accent`, `-on-rail*`, `-on-dock*`, `-link*`.
- **Bordures** — `--border-subtle`, `-default`, `-strong`, `-rail`, `-dock`, `--focus-ring`.
- **Accent et statuts** — `--accent*`, `--success|warning|danger|info` avec variantes `-soft` et `-text`.
- **Typographie** — familles, échelle `--text-3xs → --text-display`, interlignes, interlettrages, graisses, styles composés (`--font-eyebrow`, `--font-code`, `--font-meta`).
- **Forme et profondeur** — `--radius-*`, `--shadow-*`, `--blur-overlay`.
- **Mouvement** — `--ease`, `--dur-fast|base|slow`.
- **Icônes** — `--icon-size-xs → -xl`, `--icon-stroke`.
- **Contrôles** — hauteurs, `--btn-{primary,secondary,ghost,danger}-*`.
- **Géométrie du shell** — `--iconrail-width`, `--sidepanel-{default,min,max}`, `--dock-{default,min,max}`, `--topbar-height`, `--reading-width`.
