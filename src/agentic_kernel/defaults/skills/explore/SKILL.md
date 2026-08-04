---
name: explore
description: Explorer rapidement l’architecture, les symboles et les flux d’un projet.
allowed-tools:
  - codegraph_explore
  - codegraph_query
  - codegraph_node
  - codegraph_callers
  - codegraph_callees
  - codegraph_impact
  - codegraph_affected
  - codegraph_status
  - codegraph_sync
  - search_text
  - read
  - stat
amk:
  activation: on-demand
  commands:
    - /explore
  command_descriptions:
    /explore: Cartographier une architecture ou un flux sans modifier le projet.
---
# Explore

Explore le projet sans modifier son code source.

1. Transforme la demande après `/explore` en une question courte portant sur
   l’architecture, un symbole, un flux ou l’impact d’un changement.
2. Commence par `codegraph_explore`.
3. Si le résultat signale un index périmé, appelle `codegraph_sync` puis relance
   une seule fois l’exploration.
4. Utilise les tools CodeGraph spécialisés seulement si une relation précise
   reste ambiguë.
5. Si CodeGraph est absent ou ne couvre pas ce langage, utilise
   `search_text`, puis `read` sur les seuls fichiers nécessaires.
6. Ne modifie rien pendant ce tour.

Réponds avec :

- les points d’entrée probables ;
- les fichiers et symboles importants ;
- les relations ou flux observés ;
- les éléments directement établis par CodeGraph ou les lectures ;
- les inférences clairement signalées ;
- les incertitudes et la prochaine action utile.

La carte automatique du workspace est une aide de navigation, pas une preuve
de lecture. Après une modification récente, respecte tout avertissement de
fraîcheur de CodeGraph.
