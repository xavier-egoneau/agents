---
name: knowledge-capture
description: Archiver dans la bibliothèque de connaissance ce qui a une valeur durable — résultat de recherche, documentation lue, conclusion établie. Utiliser quand l'utilisateur demande de retenir quelque chose, ou quand un travail vient de produire un contenu qu'il serait coûteux de refaire.
allowed-tools:
  - knowledge_ingest
  - knowledge_search
  - knowledge_index
amk:
  activation: on-demand
  phases: [post-run]
  positive_signals: [the user asks to retain durable knowledge]
  negative_signals: [planning or building is still in progress]
  retain_until: knowledge-archived
  commands:
    - /save
  command_descriptions:
    /save: Traiter la demande, puis archiver le résultat dans la bibliothèque.
  command_prompts:
    /save: >-
      Traite d'abord la demande normalement, puis archive ce qu'elle a produit
      dans la bibliothèque de connaissance avec `knowledge_ingest`. L'archivage
      n'est pas optionnel ici : c'est ce que la commande demande. Choisis un
      titre qui nomme le sujet et non l'événement — « Anatomie des tardigrades »
      et jamais « Recherche du 8 août » — parce que ce titre désigne une page
      qui sera relue et réécrite plus tard. Si une page existe déjà sur ce
      sujet, lis-la avant de la remplacer et conserve ce qui reste vrai. Termine
      en disant ce que tu as archivé, sous quel titre et avec quels tags.
---

# Archiver ce qui mérite d'être relu

La bibliothèque est une encyclopédie : une page par sujet, réécrite quand on en
apprend davantage. Sa valeur tient à ce qu'on n'y met **pas** — une base
remplie de pages jetables cesse d'être consultée, et devient pire qu'une base
vide puisqu'on ne lui fait plus confiance.

## Ce qui mérite d'être archivé

Un contenu qu'il serait coûteux de retrouver, et qui restera vrai un moment :
documentation d'une bibliothèque, comparaison d'approches établie après
recherche, spécification, référence technique, synthèse d'un sujet exploré en
profondeur.

## Ce qui ne le mérite pas

Une réponse ponctuelle, un extrait de résultats de recherche, une information
qui sera périmée la semaine prochaine, ou ce qui appartient au projet — les
décisions et la mémoire d'un dépôt vivent dans ses `DECISION.md` et
`MEMORY.md`, pas ici.

Dans le doute, n'archive pas. Une page manquante se rattrape ; une bibliothèque
polluée se nettoie beaucoup plus difficilement.

## Nommer

Le titre désigne un **sujet**, jamais un moment. Il sert de nom de page : le
réutiliser plus tard réécrira cette page, ce qui est le comportement voulu.

Un titre daté produit une page morte que rien ne viendra jamais mettre à jour,
et la bibliothèque redevient un journal de bord.

## Classer

Emploie les tags de `library/_tags.md`. S'il en manque un, ajoute-le d'abord à
ce fichier, puis utilise-le. Une taxonomie qui dérive au fil des ingestions ne
filtre plus rien : dix étiquettes pour trois idées valent moins que trois.

## Réécrire une page existante

Lis la page avant de la remplacer. La version précédente est archivée dans le
journal, mais rien ne la rejouera pour toi : ce que tu omets disparaît de la
page courante.

Garde ce qui reste vrai, corrige ce qui ne l'est plus, ajoute ce que tu viens
d'apprendre. Une réécriture qui perd la moitié du contenu antérieur est une
régression, même si le nouveau texte est meilleur.

## Rendre compte

Dis ce que tu as archivé, sous quel titre, avec quels tags, et si tu as
remplacé une page existante. L'utilisateur doit pouvoir corriger un mauvais
classement tout de suite — plus tard, il ne saura plus où chercher.
