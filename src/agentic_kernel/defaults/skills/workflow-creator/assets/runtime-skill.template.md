---
name: WORKFLOW_ID
description: Exécuter le workflow guidé WORKFLOW_TITLE pour une routine AMK.
allowed-tools:
  - read
  # Ajouter chaque outil exact d’une étape prête, après vérification du catalogue.
---

# WORKFLOW_TITLE

Avant toute action, lire `workflow.yaml` dans la racine de cette skill.

1. Refuser l’exécution si le schéma n’est pas `amk.workflow/v1` ou si le statut
   n’est pas `ready`.
2. Résoudre les paramètres et variables depuis le contexte courant.
3. Exécuter les étapes selon leurs dépendances, sans changer d’outil, ajouter
   d’outil non déclaré ou modifier les arguments figés.
4. Appliquer uniquement les retries et fallbacks déclarés.
5. Arrêter et signaler toute déviation ou dépendance requise indisponible.
6. Conserver les preuves demandées et ne rien fabriquer pour combler une donnée
   absente.
7. Produire la sortie avec le format, les sections et la politique de preuve du
   workflow.

Ce contrat est guidé par le modèle. Le Guardian reste l’autorité de sécurité et
peut demander une autorisation avant une action sensible.
