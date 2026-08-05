# Installation et mise à jour

## Prérequis

- `uv`, qui installe Python 3.12 sans modifier le Python système;
- Node.js et npm, encore nécessaires à la surface web distribuée depuis le
  dépôt;
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

`amk setup` est idempotent. Il matérialise le socle utilisateur, réutilise
Ketch s’il est déjà accessible, l’installe sinon, puis exécute `npm ci` si les
dépendances web manquent.

Variantes :

```powershell
uv run amk setup --no-downloads  # socle seulement
uv run amk setup --full          # ajoute llama.cpp et prépare Gemma
```

Ketch, llama.cpp et Gemma déjà présents sont détectés et réutilisés. Les
variables `AMK_KETCH_BIN` et la configuration `vision.json` permettent de
pointer vers des installations non standard.

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

`doctor` renvoie un code non nul lorsqu’un composant requis manque. Les
éléments vision sont facultatifs tant qu’aucune image n’est analysée.

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
