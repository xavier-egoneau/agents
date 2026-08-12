---
name: rag-first-research
description: Réduire les recherches web répétitives en consultant d’abord la bibliothèque RAG, puis archiver sélectivement les sources publiques durables issues des recherches longues. Utiliser pour les questions factuelles, sanitaires, réglementaires, scientifiques, environnementales ou documentaires susceptibles de réutiliser des recherches antérieures.
allowed-tools:
  - knowledge_search
  - knowledge_ingest
  - web_search
  - web_scrape
  - browser_open
  - browser_snapshot
  - browser_click
  - browser_close
amk:
  activation: on-demand
  phases: [research, verify]
  positive_signals: [external current or sourced evidence can change the answer]
  negative_signals: [local implementation has sufficient repository evidence]
  retain_until: evidence-sufficient
---

# Recherche avec mémoire

## Consulter avant de rechercher

1. Reformuler le besoin en une à trois requêtes factuelles courtes.
2. Appeler `knowledge_search` avec `scope="library"` et `limit=5` avant tout outil web.
3. Réutiliser les résultats suffisamment pertinents, sourcés et encore actuels.
4. Ne lancer une recherche web que si la bibliothèque est absente, insuffisante, contradictoire ou trop ancienne pour la question.

Pour une donnée susceptible d’avoir changé — réglementation, recommandation sanitaire, alerte, météo, disponibilité ou chiffre récent — utiliser le RAG comme point de départ, puis vérifier la source officielle actuelle sur le web.

## Borner la recherche web

- Chercher autant de sources fiables que nécessaire pour établir une réponse suffisamment étayée, en privilégiant les sources primaires et canoniques.
- Essayer une URL officielle avec `web_scrape` avant d’ouvrir un navigateur.
- Utiliser le navigateur seulement si l’extraction directe échoue ou si la page exige une interaction.
- Si le backend de recherche par défaut est limité ou indisponible, essayer `backend="exa"`, puis `backend="keenable"`, avant de passer à une recherche manuelle dans le navigateur.
- Après un rate limit ou une URL obsolète, changer de backend, de source ou de méthode ; ne pas répéter plusieurs fois le même appel inchangé.
- Arrêter lorsque les preuves sont suffisantes pour répondre avec le niveau de fiabilité requis et que les nouveaux appels n’apportent plus d’information utile.
- Ne pas multiplier les sources pour rechercher une certitude absolue.

## Conserver une recherche longue

Considérer la recherche comme longue lorsqu’elle mobilise plusieurs sources primaires ou au moins cinq appels web utiles, et que ses résultats pourront servir à d’autres questions.

À la fin d’une telle recherche :

1. sélectionner une à trois sources officielles ou scientifiques réellement réutilisables ;
2. vérifier avec `knowledge_search` que la même source ou le même document n’est pas déjà archivé ;
3. appeler `knowledge_ingest` pour chaque source retenue avec :
   - son URL canonique dans `source` ;
   - un `title` précis incluant l’organisme ;
   - un `summary` court indiquant les faits réutilisables, le périmètre et la date de vérification ;
   - uniquement les tags existants `reference` et, pour un contenu à surveiller, `veille` ;
4. signaler brièvement dans la réponse que les références durables ont été mémorisées.

Ne pas archiver les résultats faibles, les pages dupliquées, les pages de résultats de recherche ni les URLs temporaires.

## Protéger les données de la personne

La bibliothèque RAG est une base de connaissances publiques. Ne pas y stocker :

- le message ou la situation personnelle de l’utilisateur ;
- une adresse, un nom, un identifiant ou une information de logement précise ;
- des symptômes, diagnostics, crises, peurs ou autres données de santé ;
- la réponse personnalisée de l’agent.

Archiver seulement des documents publics et des résumés factuels impersonnels. Lorsque la mémoire utilisateur est activée, les informations personnelles durables appartiennent à `USER.md` et les choix actés à `DECISIONS.md`, jamais au RAG.

## Répondre à partir du RAG

- Vérifier que l’extrait retrouvé s’applique réellement au cas demandé.
- Citer l’organisme, le titre, la date disponible et l’URL originale conservée dans le document.
- Distinguer clairement une information retrouvée dans la bibliothèque d’une information revérifiée aujourd’hui.
- Si aucune information nouvelle ne change la conclusion, ne pas relancer une recherche web.
