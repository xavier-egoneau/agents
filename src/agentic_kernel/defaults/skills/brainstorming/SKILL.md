---
name: brainstorming
description: Explorer un problème ouvert en produisant des options réellement distinctes avant de trancher. Utiliser quand la question est « comment aborder ceci », quand la première idée semble déjà acquise, quand il faut choisir entre plusieurs directions, ou quand une décision engage la suite du projet. Ne pas utiliser pour une tâche dont la solution est connue.
amk:
  activation: on-demand
  commands:
    - /brainstorm
  command_descriptions:
    /brainstorm: Explorer les options d'un problème ouvert avant de trancher.
---

# Explorer avant de choisir

Le risque n'est pas de manquer d'idées : c'est de s'attacher à la première.
Elle arrive vite, elle est plausible, et tout ce qui suit devient sa
justification. Cette skill sépare le moment où l'on produit des options du
moment où on les juge, parce que mélanger les deux tue les options fragiles
avant qu'elles soient formulées.

## 1. Poser le problème, pas la solution

Reformule la demande en une phrase qui ne contient aucune solution.

« Il faut ajouter un cache » est déjà une réponse. « Les mêmes données sont
relues à chaque affichage et la page attend » est un problème — et il ouvre
d'autres portes que le cache.

Nomme ce qui est contraint et ce qui ne l'est pas. Une contrainte supposée qui
n'en est pas une ferme la moitié de l'espace pour rien : dis-la à voix haute
plutôt que de l'appliquer en silence.

## 2. Diverger sans juger

Produis au moins quatre approches **structurellement différentes**. Trois
variantes du même mécanisme ne font qu'une option.

Le test : si deux propositions échouent pour la même raison, elles n'en font
qu'une. Remplaces-en une.

Pour forcer l'écart, cherche délibérément :

- l'option qui **supprime** le besoin au lieu de le servir ;
- celle qui déplace le problème ailleurs — plus tôt, plus tard, chez
  l'appelant ;
- celle qui accepte un défaut assumé en échange de beaucoup de simplicité ;
- celle qu'on n'ose pas proposer parce qu'elle remet en cause un choix
  existant.

Aucune évaluation à cette étape. Pas de « mais ». Une option manifestement
mauvaise a le droit d'exister : elle délimite l'espace et rend visible ce qui
la rend mauvaise.

## 3. Rendre les options comparables

Pour chacune, en trois lignes maximum :

- **Ce que ça fait** — le mécanisme, concrètement.
- **Ce que ça coûte** — travail, complexité durable, ce qu'on s'interdit après.
- **Ce qui la ferait échouer** — la condition précise, pas un doute vague.

C'est la troisième ligne qui compte. « Peut poser problème à grande échelle »
n'aide personne. « Casse dès qu'un deuxième processus écrit dans le même
fichier » se vérifie.

## 4. Faire remonter les hypothèses

Avant de trancher, liste les hypothèses qui, si elles étaient fausses,
changeraient le classement. Nombre d'utilisateurs, fréquence réelle, format des
données, comportement de la dépendance.

Marque celles qui sont vérifiables tout de suite. Une hypothèse vérifiable en
deux minutes ne se débat pas : elle se vérifie. C'est souvent le geste le plus
rentable de toute la session.

## 5. Trancher

Recommande **une** option et dis pourquoi elle l'emporte, pas seulement pourquoi
elle est bonne. Une recommandation qui ne bat personne n'en est pas une.

Nomme la seconde et la condition qui ferait basculer vers elle. Ce couple
« choix / condition de bascule » est ce qui reste utile dans six mois, quand le
contexte aura changé et que la décision sera à revoir.

Si aucune option ne domine, dis-le et propose ce qui départagerait — une mesure,
un essai court, une question à l'utilisateur. C'est une conclusion valable ;
choisir au hasard pour paraître décidé n'en est pas une.

## Rester honnête

Ne fabrique pas d'options pour atteindre un quota. Quatre approches faibles
valent moins que deux vraies plus l'aveu que l'espace est étroit.

Ne présente pas un compromis comme neutre. Tout choix ferme des portes : dis
lesquelles.

Ne conclus pas par une synthèse qui ménage tout le monde. La demande est de
trancher, pas de récapituler.

## Trace

Quand la discussion aboutit à une décision qui engage le projet, propose de
l'écrire dans `DECISION.md` — l'option retenue, celles écartées, et la
condition de bascule. Un choix dont on a perdu la raison sera refait à
l'identique, ou défait sans savoir ce qu'on perd.
