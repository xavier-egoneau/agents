# Workspaces et espaces personnels

## Le contrat

Le workspace est la frontière de fichiers d’un run; ce n’est ni la racine de
l’application ni la racine des données AMK.

Un run peut recevoir :

- un chemin de projet explicite;
- `null`, qui signifie « aucun projet rattaché ».

Dans le second cas, le kernel conserve `workspace: null` dans les événements,
les projections et les routines, mais fournit aux outils le chemin effectif :

```text
content-agents/workspaces/<agent_id>/
```

L’API expose `effective_workspace` et `workspace_kind`. L’interface affiche par
exemple `Espace personnel · main` et permet de parcourir ce dossier.

## Sélection au lancement

```powershell
uv run amk web --workspace C:\chemin\du\projet
uv run amk run --workspace . "Analyse ce dépôt"
```

Sans `--workspace`, `amk web` retient le CWD seulement s’il se trouve dans un
dépôt Git. Depuis un dossier trop large comme le profil utilisateur, il utilise
l’espace personnel de `main`.

## Effet sur le sandbox

`safe` étend un profil lecture seule. `limited` et `power` étendent le profil
workspace : le workspace effectif devient leur frontière d’écriture principale.
Une session globale est donc bornée au dossier personnel de son agent, jamais au
profil utilisateur complet ni au dépôt AMK par défaut.

## Connaissance de projet

`DECISION.md`, `MEMORY.md` et `notes/` appartiennent au projet et voyagent avec
lui. Ne pas inventer ces fichiers dans l’espace personnel d’une session sans
projet. La connaissance transverse au poste vit plutôt sous
`content-agents/knowledge/`.

## Projet déplacé ou supprimé

Une routine portant un chemin disparu reste visible et modifiable. La corriger
depuis le panel Automatisations en sélectionnant un projet existant ou
`Espace personnel de l’agent`. `amk crons list` signale les chemins invalides.
