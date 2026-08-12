---
name: model-context
description: Maintenir la fenêtre de contexte du modèle actif pour permettre la compaction automatique.
allowed-tools:
  - model_context_status
  - model_context_store
amk:
  activation: on-demand
  phases: [diagnose]
  positive_signals: [the active model context window is unknown]
  negative_signals: [the kernel already knows the active model context window]
  retain_until: context-window-known
---
# Fenêtre de contexte du modèle

Utilise `model_context_status` pour vérifier si la fenêtre de contexte du modèle actif est connue.

Si elle est inconnue et que la demande peut nécessiter plusieurs tours ou de nombreux tools :

1. demande à l’utilisateur la **fenêtre de contexte maximale en tokens** du modèle exact affiché ;
2. précise qu’il ne faut pas donner la limite de sortie, mais la fenêtre totale d’entrée + sortie ;
3. ne devine jamais cette valeur ;
4. après confirmation, appelle `model_context_store` avec la valeur exacte et `source="user-confirmed"`.

Si la valeur est déjà connue, ne pose aucune question. Le kernel déclenche automatiquement la compaction à 70 % ; cette skill ne doit jamais résumer ni modifier l’historique elle-même.
