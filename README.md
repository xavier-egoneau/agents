# Agentic Markdown Kernel

AMK est un kernel agentique local, modulaire et multi-agent. Il réunit une CLI,
une API et une surface web autour d’agents et de skills Markdown, d’outils
manifestés, de sessions auditables et de routines planifiées.

## Démarrage rapide

Prérequis actuels : [uv](https://docs.astral.sh/uv/), Python 3.12 géré par uv,
et Node.js/npm pour la surface web de développement.

```powershell
uv sync --extra dev --python 3.12
uv run amk setup
$amkContent = "<chemin Espace de travail affiché par amk setup>"
Copy-Item "$amkContent\providers.example.json" "$amkContent\providers.json"
$env:DEEPSEEK_API_KEY = "votre-clé"
uv run amk doctor
uv run amk web
```

Sous macOS ou Linux, affecter le chemin affiché à `amk_content`, utiliser `cp`,
puis définir la variable avec `export DEEPSEEK_API_KEY="votre-clé"`.

`amk setup` affiche le chemin réel de l’espace utilisateur. Si le projet ne
possède pas déjà un ancien `content-agents/`, ce chemin se trouve dans le
répertoire de données de la plateforme. La configuration détaillée et les
providers OAuth sont décrits dans [Configuration](docs/configuration.md).

## Trois racines à ne pas confondre

| Racine | Contenu | Règle |
|---|---|---|
| Application | code, `tools/`, `surfaces/` | indépendante du dossier de lancement |
| Données AMK | agents, skills, secrets, sessions, routines | stable et propre à l’utilisateur |
| Workspace | fichiers accessibles au run | projet choisi ou espace personnel de l’agent |

Une session dont `workspace` vaut `null` n’est rattachée à aucun projet. Le
kernel l’exécute dans `content-agents/workspaces/<agent_id>/`; `limited` et
`power` restent bornés à ce workspace effectif.

## Documentation

- [Installation et mise à jour](docs/installation.md)
- [Configuration, providers et secrets](docs/configuration.md)
- [Workspaces et espaces personnels](docs/workspaces.md)
- [Guardian et sandbox](docs/sandbox.md)
- [Routines et workflows](docs/routines.md)
- [Dépannage et lecture des logs](docs/troubleshooting.md)
- [Développement et validation](docs/development.md)
- [État courant](MEMORY.md) et [décisions d’architecture](DECISION.md)

## Commandes usuelles

```powershell
uv run amk doctor
uv run amk providers list
uv run amk providers check --provider deepseek
uv run amk agents validate
uv run amk modules check
uv run amk run --agent main --workspace . "Analyse ce projet"
uv run amk crons list
uv run amk approvals list
uv run amk web --workspace .
```

`amk web` lance l’API et la surface, arrête les anciennes instances AMK Web et
choisit un autre port web si 3000 est occupé. `Ctrl+C` arrête les deux services.

## Architecture en bref

- Les agents et skills sont décrits en Markdown.
- Les modules exécutables vivent sous `tools/modules/<id>/`; `tools/index.json`
  est un catalogue généré et vérifié.
- Le Guardian décide si une action est permise; le sandbox borne techniquement
  les processus autorisés.
- Chaque session écrit un journal append-only dans
  `content-agents/sessions/<session-id>.jsonl`.
- `content-agents/state.db` contient les projections, plans et routines.
- Les workflows de routine sont des contrats guidés, validés et versionnés;
  ils ne remplacent pas encore un moteur déterministe d’étapes.

## Développement

```powershell
uv sync --extra dev --python 3.12
uv run playwright install chromium
npm ci --prefix surfaces/web
./scripts/check.ps1
```

Sous macOS ou Linux : `./scripts/check.sh`. La validation couvre Ruff, pytest,
le frontend et les parcours Playwright. Les appels réseau réels restent opt-in.
