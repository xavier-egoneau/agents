# Dépannage et lecture des logs

## Triage rapide

Exécuter dans cet ordre :

```powershell
uv run amk doctor
uv run amk providers list
uv run amk providers check
uv run amk modules check
uv run amk agents validate
uv run amk crons list
```

`doctor` donne les chemins effectifs. Toujours diagnostiquer la racine affichée,
pas un `content-agents/` supposé dans le CWD.

## La surface Web ne démarre pas

`missing web surface` indique généralement que l’ancienne version cherchait les
assets depuis le CWD ou que `AMK_APP_ROOT` est incorrect. Lancer depuis le dépôt
ou définir `AMK_APP_ROOT` vers une racine contenant `tools/modules` et
`surfaces/web/package.json`.

Si le port 3000 est occupé, AMK choisit le suivant. Un conflit sur 8765 est
bloquant : identifier l’autre API avant de la fermer. Après une modification du
backend Python, arrêter l’ancienne instance avec `Ctrl+C`, puis relancer
`uv run amk web`.

## Provider indisponible

```powershell
uv run amk providers check --provider deepseek
uv run amk auth status codex
```

Vérifier l’`id` dans `providers.json`, la variable d’environnement dans le même
terminal que `amk web`, puis l’horloge système pour OAuth. Ne jamais coller une
clé dans un log ou une issue.

## Routine en échec

1. Lire `last_error` dans l’interface et `amk crons list`.
2. Vérifier le workspace; un ancien chemin macOS ou Windows reste visible mais
   n’est pas utilisable sur une autre machine.
3. Si le workflow est obsolète, le régénérer puis l’accepter.
4. Lancer `Tester la routine` avant `Exécuter maintenant`.
5. Ouvrir les événements de la session pour identifier la dernière transition
   ou le dernier outil.

Un échec sans `run_id` et presque instantané vient généralement de la validation
pré-exécution. Un `run_id` avec des événements `tool.started` ou
`tool.completed` indique une exécution réellement commencée.

## Recherche Web ou Ketch

```powershell
uv run amk doctor
ketch --help
```

Si `doctor` ne trouve pas Ketch, relancer `amk setup` ou définir
`AMK_KETCH_BIN`. Les limitations DuckDuckGo sont des erreurs amont temporaires;
elles ne signifient pas que le workflow ou le provider principal est invalide.

## Sandbox

Si `safe` ou `limited` refuse un processus, lire la ligne sandbox de `doctor`.
Sous Windows, exécuter `amk sandbox setup`, sélectionner le mode élevé, puis
relancer AMK. Ne pas passer en `power` uniquement pour masquer un backend absent.

## Sessions et fichiers de trace

```powershell
uv run amk session <session-id>
```

Les JSONL se trouvent sous `content-agents/sessions/`. Événements utiles :

- `session.started` : configuration logique et effective;
- `guardian.reviewed` : décision de sécurité;
- `tool.started` / `tool.completed` : frontière d’exécution;
- `run.transitioned` : état du run;
- `session.completed` : statut, sortie et erreurs structurées.

Les snapshots volumineux sont stockés sous `sessions/blobs/`. Ne pas supprimer
un JSONL ou `state.db` pendant que le serveur tourne.
