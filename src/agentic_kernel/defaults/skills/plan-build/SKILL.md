---
name: plan-build
description: Construire puis exécuter un plan persistant avec dépendances et parallélisation explicites.
allowed-tools:
  - plan_create
  - plan_status
  - plan_ready
  - plan_claim
  - plan_update
  - subagent_spawn
amk:
  activation: on-demand
  commands:
    - /plan
    - /build
  command_descriptions:
    /plan: Construire un plan structuré avec tâches, dépendances et parallélisation.
    /build: Exécuter le plan courant, vérifier et cocher chaque tâche.
---
# Plan et build

La commande placée au début du message choisit strictement le mode.

## `/plan`

Construis une stratégie concrète pour répondre à la demande, sans l’implémenter.

1. Inspecte seulement ce qui est nécessaire pour comprendre le workspace.
2. Découpe le résultat en tâches petites, vérifiables et orientées résultat.
3. Attribue un identifiant stable `T1`, `T2`, etc.
4. Renseigne pour chaque tâche ses `dependencies`.
5. Mets `parallelizable: true` uniquement si la tâche peut être confiée sans
   dépendance non terminée, sans modifier les mêmes fichiers qu’une autre tâche
   parallèle et avec un résultat réintégrable sans ambiguïté.
6. Pour chaque tâche parallélisable, renseigne obligatoirement `write_scopes`
   avec les chemins précis qu’elle est autorisée à modifier.
7. Appelle `plan_create` avec cette structure. Le plan est la source de vérité.
8. Présente ensuite le plan en Markdown avec des cases `- [ ]`, ses dépendances
   et le marqueur `— parallélisable` lorsque pertinent.

Ne modifie aucun fichier métier et ne lance aucune commande d’installation ou
de build en mode `/plan`.

## `/build`

Applique le plan courant, sans en inventer un nouveau.

1. Appelle `plan_status` sans identifiant pour charger le plan le plus récent.
2. Appelle `plan_ready` avant chaque vague d’exécution. Seules les tâches
   retournées par ce tool sont exécutables.
3. Avant une tâche séquentielle, réserve-la avec `plan_claim`, puis passe-la à
   `in_progress` avec `plan_update`.
4. Exécute et vérifie réellement la tâche, puis passe-la à `validating`, réalise
   les contrôles attendus et passe-la à `completed` avec une
   note factuelle. Utilise `blocked` ou `failed` avec la cause exacte sinon.
5. Pour une tâche retournée dans `parallel`, tu peux appeler `subagent_spawn`.
   Transmets obligatoirement son `plan_id` et son `step_id`; le kernel prend le
   lease et contrôle les conflits de `write_scopes`. Donne-lui à la volée un
   rôle, une mission, un périmètre et le résultat attendu. Plusieurs appels
   indépendants peuvent être émis dans le même tour afin que le kernel les
   exécute en parallèle. Continue les autres tâches prêtes qui n’en dépendent pas.
6. Ne coche jamais une tâche sur la seule déclaration d’un sous-agent :
   réintègre son résultat et vérifie les artefacts ou tests attendus.
7. Termine lorsque toutes les tâches sont terminées ou qu’aucune tâche restante
   n’est exécutable. Dans ce dernier cas, explique le blocage.

Après chaque changement de statut, le panneau de plan Web se met à jour depuis
l’état persistant. Le texte de réponse reste un compte rendu, pas la source de
vérité du statut.

## Itérations après livraison

Un plan entièrement terminé est archivé : il ne redevient jamais le plan actif
et `/reprise` ne doit pas tenter de le rejouer.

Quand l’utilisateur formule ensuite un retour, une correction ou une demande
d’amélioration, traite ce message comme une nouvelle itération sur les artefacts
livrés :

1. conserve la même session et son contexte utile ;
2. inspecte l’état réel du workspace au lieu de te fier au compte rendu final ;
3. applique directement une correction ciblée si elle est courte ;
4. si la révision comporte plusieurs tâches, crée un nouveau plan différentiel
   contenant uniquement le travail restant, puis exécute-le lorsque la demande
   implique clairement une modification ;
5. ne rouvre pas et ne réinitialise pas les tâches déjà validées de l’ancien
   plan.
