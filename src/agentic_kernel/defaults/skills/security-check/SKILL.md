---
name: security-check
description: Contrôler la sécurité de la machine et rendre compte de ce qui a changé depuis le dernier passage. Utiliser pour la routine quotidienne de sécurité, ou quand l'utilisateur demande de vérifier ce qui tourne, ce qui démarre avec la machine, ce qui sort sur le réseau, ou quels serveurs MCP sont déclarés.
allowed-tools:
  - sentinel_scan
  - sentinel_acknowledge
amk:
  activation: on-demand
  commands:
    - /secu
  command_descriptions:
    /secu: Lancer un contrôle de sécurité et rapporter uniquement les écarts.
  command_prompts:
    /secu: >-
      Lance `sentinel_scan` puis rends compte selon la méthode de cette skill.
      Ne réclame pas de confirmation avant de scanner : c'est ce que la commande
      demande.
---

# Rendre compte d'un écart, pas d'un inventaire

Sentinel ne renvoie pas l'état de la machine, il renvoie **ce qui a changé**
depuis le dernier passage. Cette distinction commande toute la restitution.

Un rapport qui réénumère chaque jour les mêmes quarante processus ne sera pas lu
la troisième fois. Et le jour où quelque chose de réellement anormal s'y trouve,
il ne sera pas lu non plus. La valeur de cette routine tient entièrement à sa
capacité à se taire.

## Méthode

1. Appeler `sentinel_scan`. Ne pas restreindre les sections sans raison :
   l'intérêt vient du recoupement entre elles.
2. Si `baseline_established` vaut `true`, c'est le premier passage. Il n'y a
   rien à signaler — dire en une phrase que la référence vient d'être établie et
   que les prochains scans porteront sur les écarts. Ne pas énumérer ce qui a
   été trouvé : rien n'y est suspect, tout y est simplement inédit.
3. Sinon, ne traiter que `new`. `known_count` et `acknowledged_count` sont des
   chiffres de contexte, pas une matière à commenter.
4. Toujours restituer `limitations`. Une section non couverte doit être dite,
   sinon le silence se lira comme une absence de problème.

## Ce qu'un écart veut dire

Aucune observation n'est une menace du seul fait d'être nouvelle. Installer un
logiciel crée des processus, des persistances et des connexions ; c'est le cas
le plus fréquent, de loin.

Pour chaque écart, énoncer trois choses : **ce qui est apparu**, **l'explication
banale la plus probable**, et **ce qui reste à vérifier** pour trancher. Ne
jamais conclure à une compromission — proposer la vérification.

Quelques repères d'attention, sans automatisme :

- Un exécutable **non signé** hors des dossiers système mérite d'être nommé.
- Une **persistance** est le point d'ancrage obligé de ce qui veut survivre au
  redémarrage : c'est la section au meilleur rapport signal/bruit.
- Une **écoute sur toutes les interfaces** expose un service au réseau local.
- Un **serveur MCP** nouvellement déclaré est du code exécuté avec les droits de
  l'utilisateur et un accès direct au contexte de l'agent. Il échappe à
  l'antivirus, qui ne voit qu'un interpréteur légitime lançant un script
  légitime.

## Acquitter

Quand l'utilisateur confirme qu'un écart est normal, appeler
`sentinel_acknowledge` avec son empreinte et une note courte disant pourquoi.
L'écart cesse alors de remonter, définitivement.

Ne jamais acquitter de sa propre initiative. Un acquittement automatique
reviendrait à décider seul de ne plus regarder — exactement l'inverse du but.

## Ce que ce contrôle ne fait pas

La détection au niveau du système arrive après coup : elle mesure la durée d'une
fuite, elle ne l'empêche pas. Le dire quand la question se pose, et rappeler que
les couches qui agissent plus tôt sont ailleurs — un jeton matériel pour que les
secrets ne passent jamais par le clavier, la surveillance des sorties réseau
pour qu'une capture ne puisse pas être transmise, et la séparation des usages
pour qu'une machine compromise n'en livre pas d'autres.

Sur Windows en particulier, aucune énumération des hooks clavier n'est possible
en espace utilisateur. Ne pas laisser croire que cette section est couverte.
