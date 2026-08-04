---
name: dev
description: Guider toute écriture, correction, refactorisation ou revue de code vers la solution correcte la plus simple, avec un diff chirurgical et une validation proportionnée. Utiliser pour les tâches de développement, les choix d’architecture ou de dépendances, et chaque fois qu’une solution paraît surdimensionnée, abstraite, répétitive ou inutilement longue.
---

# Développer avec le minimum de code correct

Écrire moins de code, pas moins de réflexion. Comprendre le flux réel avant de choisir la solution la plus courte.

## Comprendre avant de modifier

1. Lire la demande, les fichiers concernés et les conventions du projet.
2. Pour un bug, reproduire le symptôme et remonter jusqu’à la cause commune. Vérifier les appelants avant de modifier une fonction partagée.
3. Énoncer brièvement les hypothèses qui changeraient la solution. Ne demander une clarification que si une hypothèse raisonnable serait risquée.

## Monter l’échelle de simplicité

S’arrêter au premier niveau qui résout entièrement le besoin :

1. Ne rien ajouter si le comportement existe déjà ou si le besoin est spéculatif.
2. Réutiliser un helper, un type ou un pattern déjà présent dans le projet.
3. Utiliser la bibliothèque standard.
4. Utiliser une capacité native de la plateforme, du navigateur ou de la base de données.
5. Utiliser une dépendance déjà installée.
6. Écrire la ligne ou le petit bloc évident.
7. Seulement ensuite, créer le minimum de nouveau code nécessaire.

Si 100 lignes peuvent être remplacées par une ligne aussi lisible, correcte et maintenable, garder la ligne. Ne pas compresser du code au prix de la clarté.

## Garder le changement chirurgical

- Relier chaque ligne modifiée à la demande.
- Respecter le style et les frontières existantes.
- Ne pas refactoriser, renommer ou reformater du code voisin sans nécessité.
- Supprimer les imports, variables et branches rendus inutiles par son propre changement.
- Éviter une abstraction à une seule implémentation, une configuration pour une constante, un wrapper sans comportement et une dépendance pour quelques lignes triviales.
- Tolérer une petite duplication avant de créer prématurément une mauvaise abstraction. Extraire quand le même concept est réellement stable et répété.

## Ne jamais simplifier les garanties

Ne pas retirer ou contourner :

- validation aux frontières de confiance ;
- sécurité, permissions et protection des secrets ;
- gestion d’erreur qui prévient perte ou corruption de données ;
- accessibilité essentielle ;
- compatibilité explicitement requise ;
- comportement demandé par l’utilisateur.

La solution minimale doit rester complète sur les cas réels, pas seulement sur le chemin heureux.

## Vérifier avant de conclure

1. Définir un résultat observable avant de coder.
2. Pour un bug, ajouter ou identifier un test qui échoue avant la correction.
3. Exécuter la validation la plus petite qui prouve le changement, puis élargir selon le risque.
4. Ne jamais annoncer un succès sans preuve récente.

Pour une logique non triviale, laisser au moins un contrôle reproductible. Pour une modification triviale déjà couverte, ne pas créer une infrastructure de test disproportionnée.

## Tenir la mémoire du projet

Une session ne conserve rien après sa fermeture. Ce qui doit survivre vit en
markdown versionné, à la racine du workspace :

- `DECISION.md` — journal des choix structurants. **Ajouter** une entrée
  numérotée quand une décision engage la suite : contrat d'interface, modèle de
  données, règle transverse, dépendance. Écrire ce qui a été décidé *et*
  pourquoi, pour qu'un lecteur puisse contredire le raisonnement. Ne jamais
  réécrire une décision passée : en ajouter une nouvelle qui la remplace.
- `MEMORY.md` — état courant du projet. **Mettre à jour** ce qui devient faux :
  composants livrés, points d'entrée, dette assumée. Un état périmé nuit plus
  qu'un fichier absent, car il sera lu comme vrai.
Au démarrage d'une tâche non triviale sur un projet inconnu, lire `DECISION.md`
et `MEMORY.md` avant d'explorer le code. En fin de tâche, vérifier si le
changement en a rendu une partie fausse — et la corriger dans le même diff.

Ne rien créer sur un projet qui n'en a pas l'usage. Une session sans workspace
n'a aucun de ces fichiers : s'en tenir alors au contexte de la conversation.

Pour retrouver une information dans un corpus volumineux, `knowledge_index` puis
`knowledge_search` avec `scope="project"` donnent des extraits cités ligne à
ligne. La bibliothèque transverse du poste — documents ingérés, hors dépôt —
répond au même outil avec `scope="library"`.

## Rendre les compromis visibles

Lorsque la version simple écarte volontairement une extension spéculative, le signaler en une phrase :

`Implémenté : <solution minimale>. Écarté : <complexité inutile>; à ajouter seulement si <condition mesurable>.`
