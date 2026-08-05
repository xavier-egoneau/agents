# Skills utilisateur

Ce dossier sera utilisé pour ranger les skills ou capacités définies côté utilisateur.

## Objectif

Permettre d’ajouter progressivement des modules spécifiques sans coupler l’application à leurs détails internes.
# Skills et commandes RPPL

Une skill est un dossier contenant au minimum `SKILL.md`. Elle peut déclarer
des commandes `/...` dans son front matter :

```yaml
---
name: exemple
description: Description affichée dans AMK.
amk:
  commands:
    - /exemple
  command_descriptions:
    /exemple: Description affichée dans l’autocomplétion.
---
```

AMK charge d’abord les skills globales de `content-agents/skills/`, puis cumule
les skills locales du workspace courant découvertes dans :

- `content-agents/skills/`
- `.amk/skills/`
- `.agents/skills/`
- `.claude/skills/`
- `.codex/skills/`

Une définition locale portant le même nom ou la même commande remplace la
définition globale uniquement pour ce workspace. Les commandes apparaissent
dans le composer dès que le message commence par `/`.

Le kernel fournit aussi des commandes RPPL natives indépendantes des skills.
`/reprise` retrouve la prochaine action utile dans la session courante sans
rejouer les opérations déjà terminées.
