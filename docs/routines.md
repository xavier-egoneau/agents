# Routines et workflows

## Créer et gérer une routine

La surface **Automatisations** est l’interface complète de création, édition,
activation, test et suppression. La CLI expose actuellement l’inspection :

```powershell
uv run amk crons list
```

Chaque routine possède un prompt, un agent, des skills, un mode de sécurité, un
workspace optionnel, une destination de résultat et une expression cron.

## Workspace

Pour une veille, un agenda ou une autre tâche sans projet, choisir
`Espace personnel de l’agent`. La valeur persistée reste `null`; le run utilise
`content-agents/workspaces/<agent_id>/`. Un chemin hérité d’une autre machine
doit être corrigé avant l’exécution.

## Workflow guidé

Le workflow est facultatif. Son cycle est :

1. générer une proposition à partir du prompt, des skills et du catalogue;
2. vérifier les étapes, outils, dépendances et permissions;
3. accepter explicitement la proposition;
4. tester la routine pour prévalider ses actions;
5. activer ou laisser active la routine.

Un workflow accepté fixe une liste d’outils et guide fortement l’agent, mais AMK
ne possède pas encore de runner déterministe garantissant l’ordre métier.

Le hash de base inclut la configuration, les skills, les schémas d’outils et les
métadonnées de sécurité Guardian. Après une évolution de ce contrat, un ancien
workflow est déclaré obsolète et doit être régénéré.

## Fuseau horaire

Le cron est calculé dans `execution.timezone` du workflow,
`Europe/Paris` par défaut. Le scheduler convertit les références UTC vers ce
fuseau avant chaque calcul afin de préserver l’heure locale et les changements
heure d’été/hiver.

## Prévalidation et résultats

`Tester la routine` exécute le même contrat sans consommer l’échéance planifiée.
Le test détecte les autorisations nécessaires et met à jour le dernier statut.
Une occurrence planifiée conserve sa propre ligne durable et livre son résultat
dans la session choisie, par défaut la boîte globale Routines.

États importants :

- `success` : exécution terminée;
- `failed`, `timeout`, `partial` : échec ou résultat incomplet;
- `approval_pending` / `blocked` : intervention nécessaire;
- `in_flight` : occurrence en cours.

## Logs

- définition et état : `content-agents/state.db`, tables `cron_jobs` et
  `cron_runs`;
- trace complète :
  `content-agents/sessions/<session-id>.jsonl`;
- inspection validée d’une session : `amk session <session-id>`.

Ne pas modifier SQLite à la main. Utiliser l’interface; réserver la lecture
directe de la base au diagnostic.

Voir [Dépannage](troubleshooting.md) pour distinguer un échec de workflow, de
provider, d’outil ou de workspace.
