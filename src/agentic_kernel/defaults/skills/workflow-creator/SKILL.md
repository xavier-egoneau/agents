---
name: workflow-creator
description: Concevoir, auditer ou réviser des workflows explicites pour les routines AMK. Utiliser ce skill pour transformer un objectif récurrent trop libre en procédure versionnée avec outils exacts, paramètres, dépendances, contrôles, fallbacks, permissions et format de sortie, ou pour diagnostiquer une routine dont l’exécution varie trop.
allowed-tools:
  - tool_search
  - tool_describe
  - read
  - list
  - stat
  - mkdir
  - copy
  - write
  - patch
  - command_run
---

# Workflow Creator

Créer le contrat d’exécution optionnel d’une routine. Utiliser ce skill pendant
la conception ou la modification, jamais comme workflow exécuté à chaque
passage.

## Choisir le mode de création

- En mode `proposal`, retourner uniquement un document `amk.workflow/v1`
  structuré. Ne créer aucun fichier, aucune session, aucune permission et ne
  modifier aucune routine. L’interface persiste ce document dans la routine
  seulement après validation explicite de l’utilisateur.
- En mode `package`, uniquement sur demande explicite de réutilisation, générer
  une skill d’exécution autonome et versionnée avec son `workflow.yaml`.

Utiliser `proposal` par défaut pour le formulaire de création d’une routine.
L’absence de workflow est un état valide : la routine s’exécute alors librement
avec son prompt et ses skills sélectionnées.

## Distinguer les niveaux de contrôle

- Considérer un prompt seul comme un objectif à forte liberté : l’agent choisit
  les outils, leur ordre, leurs paramètres et ses stratégies de repli.
- Considérer un workflow accepté comme un workflow guidé : ses instructions et
  sa liste d’outils cadrent fortement le modèle, mais ne constituent pas encore
  un moteur déterministe de l’ordre métier.
- Ne jamais annoncer une garantie stricte. AMK ne possède pas encore de runner
  de workflow ; le Guardian contrôle les risques, pas l’ordre métier.

Ne jamais rattacher `workflow-creator` à la routine finale. En mode `proposal`,
la routine possède directement le workflow accepté, séparément de ses skills.

## Concevoir un workflow

1. Relever l’objectif, la fréquence, le fuseau horaire, le workspace, la
   destination du résultat et les critères de réussite de la routine.
2. Séparer les invariants des choix réellement adaptatifs. Fixer l’outil,
   l’ordre et les arguments lorsque la répétabilité compte ; laisser une marge
   seulement lorsque le contexte la justifie.
3. Rechercher chaque capacité réellement exigée par l’objectif avec
   `tool_search`, puis vérifier son contrat avec `tool_describe`. Ne jamais
   inventer un nom d’outil ou ses paramètres. Ne pas transformer les exemples
   donnés pendant la discussion en dépendances de la routine.
4. Si une capacité manque, la placer dans `missing_dependencies`, marquer le
   workflow `blocked` et expliquer ce qui doit être connecté. Ne pas substituer
   silencieusement un autre outil.
5. En mode `proposal`, utiliser directement le schéma structuré fourni par le
   serveur ; ne tenter ni de lire ni d’écrire les ressources du package. En
   mode `package`, lire
   [references/workflow-format.md](references/workflow-format.md), puis partir
   de [assets/workflow.template.yaml](assets/workflow.template.yaml).
6. Décrire chaque étape avec un identifiant stable, ses dépendances, l’outil
   exact, les arguments gabarits, le résultat conservé, les preuves attendues,
   le retry borné et le fallback explicite.
7. Déclarer les outils dans `permissions.declarations`. Conserver
   `authority: kernel_guardian` : le workflow documente les permissions, il ne
   les accorde jamais.
8. Ajouter une étape de synthèse qui ne s’appuie que sur les résultats capturés.
   Interdire la fabrication de données lorsqu’une étape requise échoue.
9. En mode `proposal`, retourner le workflow structuré sans l’écrire. En mode
   `package`, créer une skill dédiée à partir de
   [assets/runtime-skill.template.md](assets/runtime-skill.template.md) et
   garder `workflow.yaml` à côté de son `SKILL.md`.
10. Valider avant livraison :

    `python scripts/validate_workflow.py chemin/vers/workflow.yaml`

11. Présenter le workflow, les dépendances manquantes et les permissions qui
    seront demandées. Indiquer explicitement qu’il n’est pas encore enregistré
    tant que l’utilisateur ne l’a pas accepté.

## Règles de génération

- Utiliser `schema: amk.workflow/v1` et `execution.mode: agent_guided`.
- Conserver `execution.deviation: stop_and_report` et
  `permissions.unlisted: stop_and_report` par défaut.
- Résoudre la date depuis le contexte d’exécution ; ne pas figer la date de
  création du workflow.
- Borner chaque retry à trois tentatives maximum.
- Exiger une preuve exploitable pour toute étape outillée.
- Dériver la liste d’outils d’exécution uniquement des étapes outillées. Une
  liste vide signifie qu’aucun outil n’est nécessaire ; elle ne signifie pas
  « tous les outils ».
- En mode `package`, garder `allowed-tools` aligné avec les étapes et utiliser
  des identifiants versionnés. Ne jamais y laisser un nom fictif.
- Ne jamais inclure de secret ou de jeton. Référencer seulement un nom de secret
  via le mécanisme prévu par l’outil.
- Ne pas modifier ni activer une routine sans validation explicite. La
  proposition, son acceptation, sa suppression et l’activation sont des actions
  distinctes.

## Livrer

Donner un bilan court contenant :

- le degré de liberté restant ;
- les étapes et outils figés ;
- les dépendances absentes ;
- les permissions attendues ;
- l’état `proposé`, `accepté` ou `bloqué` ;
- la limite suivante : skill guidée ou besoin d’un runner strict.
