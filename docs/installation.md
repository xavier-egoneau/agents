# Installation et mise à jour

## Prérequis

- `uv`, qui installe Python 3.12 sans modifier le Python système;
- Node.js et npm, nécessaires à la surface web et à l’installation de
  CodeGraph (`amk setup` s’en charge);
- facultatif : Codex CLI pour son helper sandbox, et llama.cpp/Gemma pour la
  vision locale.

## Installation depuis le dépôt

PowerShell :

```powershell
uv sync --extra dev --python 3.12
uv run amk setup
```

macOS ou Linux :

```bash
uv sync --extra dev --python 3.12
uv run amk setup
```

`amk setup` est idempotent. Il matérialise le socle utilisateur, puis pour
chaque outil : réutilise ce qui est déjà accessible, l’installe sinon.

- **Ketch** — binaire managé, récupéré depuis ses releases GitHub.
- **CodeGraph** — CLI d’exploration sémantique de projet (skill `explore`),
  installée globalement via `npm install --global @colbymchenry/codegraph`.
- **Dépendances web** — `npm ci` si `surfaces/web/node_modules` manque.
- **SearXNG** — service de recherche loopback-only, source figée à une
  révision précise et vérifiée par somme SHA-256.

Variantes :

```powershell
uv run amk setup --no-downloads  # socle seulement
uv run amk setup --full          # ajoute aussi llama.cpp et prépare Gemma
```

Ketch, CodeGraph, SearXNG, llama.cpp et Gemma déjà présents sont détectés et
réutilisés. Les variables `AMK_KETCH_BIN` et `AMK_CODEGRAPH_BIN` permettent de
pointer vers des installations non standard de ces deux outils; la
configuration `vision.json` fait de même pour llama.cpp/Gemma.

## Configuration initiale

`setup` affiche la racine de données réellement utilisée. Copier ensuite le
gabarit du provider dans cette racine.

PowerShell, pour un checkout qui conserve son `content-agents/` local :

```powershell
$amkContent = "<chemin Espace de travail affiché par amk setup>"
Copy-Item "$amkContent\providers.example.json" "$amkContent\providers.json"
$env:DEEPSEEK_API_KEY = "votre-clé"
```

macOS ou Linux :

```bash
amk_content="<chemin Espace de travail affiché par amk setup>"
cp "$amk_content/providers.example.json" "$amk_content/providers.json"
export DEEPSEEK_API_KEY="votre-clé"
```

Voir [Configuration](configuration.md) pour OAuth et les secrets.

## Vérification et lancement

```powershell
uv run amk doctor
uv run amk providers check --provider deepseek
uv run amk modules check
uv run amk agents validate
uv run amk web
```

`doctor` renvoie un code non nul lorsqu’un composant requis manque. Ketch,
CodeGraph et SearXNG apparaissent en `missing` s’ils n’ont pas encore été
installés (`amk setup` les corrige), sans bloquer le code de sortie : chaque
capacité se dégrade proprement en leur absence. Les éléments vision restent
facultatifs tant qu’aucune image n’est analysée.

## Emplacement des données

Une installation neuve utilise :

- Windows : `%LOCALAPPDATA%\amk\content-agents`;
- macOS : `~/Library/Application Support/amk/content-agents`;
- Linux : `$XDG_DATA_HOME/amk/content-agents` ou
  `~/.local/share/amk/content-agents`.

Un checkout contenant déjà `content-agents/` le conserve pour ne pas abandonner
ses sessions et credentials. Surcharges disponibles :

- `AMK_APP_ROOT` : racine des assets applicatifs;
- `AMK_HOME` : parent explicite de `content-agents/`;
- `AMK_DATA_HOME` : répertoire de données de la plateforme.

## Mise à jour

Après une mise à jour du dépôt :

```powershell
uv sync --extra dev --python 3.12
uv run amk setup
uv run amk doctor
```

Le manifeste `.amk-defaults.json` met à jour les fichiers livrés restés
inchangés, restaure ceux qui manquent et préserve toute variante utilisateur.
Ne pas copier tout `content-agents/` entre machines : il contient secrets,
sessions et chemins de routines propres au poste.
