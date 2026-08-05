---
name: plan-build
description: Construire puis exécuter un plan persistant avec dépendances et parallélisation explicites.
allowed-tools:
  - plan_create
  - plan_status
  - plan_ready
  - plan_claim
  - plan_update
  - agent_delegate
  - subagent_spawn
amk:
  activation: on-demand
  commands:
    - /plan
    - /build
  command_descriptions:
    /plan: Construire un plan structuré avec tâches, dépendances et parallélisation.
    /build: Exécuter le plan courant, vérifier et cocher chaque tâche.
  command_prompts:
    /plan: >-
      Produis un plan et arrête-toi là. Tu es en mode planification : appelle
      `plan_create` puis rends la main. N'exécute aucune tâche, ne modifie aucun
      fichier du projet, ne lance aucune commande d'installation ou de build,
      même si le plan te paraît évident et même si l'utilisateur semble pressé.
      L'utilisateur doit pouvoir relire et corriger le plan avant qu'il ne
      produise le moindre effet; c'est la seule occasion qu'il en aura.
      Termine en présentant le plan et en indiquant que `/build` l'exécutera.
    /build: >-
      Exécute le plan déjà enregistré, n'en crée pas un nouveau. Charge-le avec
      `plan_status`, puis appelle `plan_ready` avant chaque vague : seules les
      tâches qu'il retourne sont exécutables, et lui seul distingue ce qui peut
      partir en parallèle de ce qui doit attendre. Confie chaque tâche du groupe
      `parallel` à un agent existant avec `agent_delegate`, en transmettant
      `plan_id` et `step_id`, et émets ces appels dans le même tour — les
      enchaîner l'un après l'autre annule tout le bénéfice de la parallélisation
      déclarée au moment du plan. Choisis l'agent selon la nature de la tâche;
      ne recours à `subagent_spawn` que si aucun agent ne convient. Ne coche une
      tâche qu'après avoir vérifié son résultat.
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
5. Confie chaque tâche du groupe `parallel` à un **agent existant** avec
   `agent_delegate`, en transmettant `plan_id` et `step_id`. Le kernel prend
   alors le bail et refuse toute tâche dont le périmètre d’écriture recouvre
   celui d’une tâche déjà en cours — c’est ce qui rend le parallélisme sûr.
   Émets ces appels **dans le même tour** : les enchaîner l’un après l’autre
   annule tout le bénéfice de la parallélisation déclarée au moment du plan.
6. Choisis l’agent d’après la nature de la tâche, pas au hasard. Les agents
   disponibles sont listés dans tes instructions avec leur description; chacun
   a des outils et des consignes calibrés pour son rôle. N’emploie
   `subagent_spawn` que si aucun d’eux ne convient : son exécutant est créé
   pour l’occasion, sans outillage adapté ni consigne relue.
7. Rappelle-toi qu’un agent démarre sur un contexte vide et ne voit ni cette
   conversation ni le plan. Sa consigne doit contenir les chemins, la
   contrainte et le critère d’acceptation.
8. Ne coche jamais une tâche sur la seule déclaration d’un agent : réintègre
   son résultat et vérifie les artefacts ou tests attendus.
9. Termine lorsque toutes les tâches sont terminées ou qu’aucune tâche restante
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
