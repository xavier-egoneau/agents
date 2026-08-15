# Kanban de projet

Chaque workspace possède un tableau durable stocké dans `content-agents/state.db`.
Une carte contient un prompt réutilisable, des critères d’acceptation, une
priorité, un agent et ses paramètres d’exécution.

Les colonnes sont `backlog`, `ready`, `running`, `review`, `done` et `blocked`.
Une tâche `ready` ne peut pas être vide. Le bouton **Utiliser** copie son prompt
dans le composer sans l’exécuter.

## Dispatcher cron

Le module `kanban` est activé pour l’agent principal. Une routine cron peut être
rattachée au workspace du projet avec le prompt suivant :

```text
Appelle kanban_claim_next avec automatic_only=true. S’il n’y a aucune tâche,
termine sans autre action. Sinon exécute exactement le prompt de la carte dans
le workspace courant, vérifie ses critères d’acceptation, puis appelle
kanban_finish avec review si le travail doit être relu, done s’il est entièrement
validé, ou blocked avec l’erreur et la condition de déblocage.
```

La réservation est atomique et possède un lease : deux routines ne peuvent pas
prendre la même carte. Un lease expiré replace automatiquement la tâche dans
`ready`. Seules les cartes marquées **Exécution automatique** sont réclamées par
le dispatcher par défaut.

Les outils disponibles sont `kanban_list`, `kanban_create`, `kanban_update`,
`kanban_claim_next` et `kanban_finish`.
