# Développement et validation

## Structure

- `src/agentic_kernel/` : kernel, API, scheduler, sécurité et providers;
- `src/agentic_kernel/defaults/` : socle livré à `amk init/setup`;
- `tools/modules/` : modules et manifestes;
- `tools/index.json` : catalogue dérivé, jamais édité comme source;
- `surfaces/web/` : interface Next.js/React;
- `tests/` : tests Python;
- `DECISION.md` : raisons et changements de contrat;
- `MEMORY.md` : état courant et dette connue.

## Environnement

```powershell
uv sync --extra dev --python 3.12
uv run playwright install chromium
npm ci --prefix surfaces/web
```

## Validation complète

PowerShell :

```powershell
./scripts/check.ps1
```

macOS ou Linux :

```bash
./scripts/check.sh
```

Commandes ciblées :

```powershell
uv run ruff check .
uv run pytest -q
uv run amk modules build-index
uv run amk modules check
npm run lint --prefix surfaces/web
npm run test:unit --prefix surfaces/web
npm run build --prefix surfaces/web
```

Une modification de fonction d’outil peut changer son schéma. Reconstruire alors
`tools/index.json`, vérifier les workflows concernés et ajouter un test de
contrat. Les métadonnées `path_parameters` et `url_parameters` participent à la
politique Guardian et à la base versionnée des workflows.

## Fichiers utilisateur

Ne pas coder un chemin absolu vers `content-agents/`. Utiliser `ProjectConfig`
et les helpers de `paths.py`. Les fichiers sous `content-agents/` appartiennent
à l’utilisateur et sont normalement ignorés par Git.

Pour faire évoluer une skill ou un gabarit livré, modifier sa source sous
`src/agentic_kernel/defaults/`, puis exécuter `amk init`. Le manifeste ne
remplace que les copies restées inchangées; un contenu personnalisé est préservé.

## Documentation et décisions

Le README reste une porte d’entrée. Ajouter les procédures dans `docs/`, l’état
présent dans `MEMORY.md` et toute décision durable dans `DECISION.md`. Une
décision remplacée reste dans l’historique avec une mention explicite; elle ne
doit pas continuer à documenter une commande supprimée comme si elle existait.
