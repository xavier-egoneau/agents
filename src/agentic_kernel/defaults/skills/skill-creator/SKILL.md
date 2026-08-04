---
name: skill-creator
description: Créer, auditer ou améliorer les skills embarquées d’AMK dans content-agents/skills. Utiliser ce skill quand l’utilisateur demande d’ajouter une capacité réutilisable à son agent, de transformer une méthode répétée en skill, de corriger une skill existante ou d’en valider le package et le déclenchement.
allowed-tools:
  - read
  - list
  - stat
  - mkdir
  - write
  - patch
  - command_run
---

# Skill Creator

Créer des capacités réellement utilisables par l’agent AMK. Travailler uniquement
dans `content-agents/skills/`, sauf si l’utilisateur désigne explicitement une
skill locale d’un workspace.

## Concevoir

1. Identifier les demandes qui doivent déclencher la skill et deux ou trois
   exemples concrets d’utilisation. Déduire les cas raisonnables lorsque le
   besoin est clair ; poser une question seulement si une ambiguïté change
   matériellement la capacité créée.
2. Inspecter les skills existantes et les outils réellement disponibles. Ne pas
   dupliquer une capacité déjà couverte et ne jamais inventer un outil.
3. Choisir un nom court en minuscules, chiffres et tirets, inférieur à 64
   caractères. Nommer le dossier exactement comme la skill.
4. Déterminer le degré de liberté adapté : instructions souples pour un travail
   exploratoire, procédure précise et script déterministe pour une opération
   fragile ou répétitive.

## Créer le package

Créer au minimum :

```text
content-agents/skills/<nom>/
├── SKILL.md
└── agents/openai.yaml
```

Ajouter seulement les dossiers utiles :

- `scripts/` pour une opération déterministe et répétée ;
- `references/` pour les connaissances détaillées chargées à la demande ;
- `assets/` pour les modèles et fichiers utilisés dans les livrables.

Ne pas ajouter de README, journal de modifications ou documentation auxiliaire.

## Écrire SKILL.md

- Limiter le front matter à `name`, `description` et, pour AMK,
  `allowed-tools` lorsque la skill a besoin d’outils.
- Faire de `description` le mécanisme de déclenchement : dire ce que fait la
  skill et dans quelles demandes elle doit être chargée.
- Écrire le corps à l’infinitif ou à l’impératif, pour une autre instance de
  l’agent. N’inclure que les connaissances non évidentes et les garde-fous
  nécessaires.
- Garder `SKILL.md` concis. Déplacer les variantes et longues références dans
  `references/`, avec un lien direct depuis `SKILL.md` et une indication claire
  du moment où les lire.
- Résoudre tous les chemins relatifs depuis la racine de la skill.
- Donner à la skill uniquement les outils nécessaires. Une déclaration
  `allowed-tools` ne contourne jamais le Guardian ni le niveau de permission de
  la session.

## Écrire les métadonnées UI

Créer `agents/openai.yaml` avec des chaînes entre guillemets :

```yaml
interface:
  display_name: "Nom lisible"
  short_description: "Description courte de 25 à 64 caractères"
  default_prompt: "Utilise $nom-de-skill pour ..."
```

Le prompt par défaut doit mentionner explicitement `$nom-de-skill`. Ne pas
ajouter d’icône, de couleur ou de dépendance non fournie par l’utilisateur.

## Modifier sans casser

Avant de modifier une skill existante, lire son `SKILL.md` et ses ressources
directement référencées. Préserver les comportements utiles et les changements
de l’utilisateur. Ne jamais écraser silencieusement un dossier existant :
mettre à jour les fichiers ciblés ou demander confirmation si la demande
implique un remplacement incompatible.

## Valider et livrer

Après chaque création ou modification substantielle, exécuter depuis la racine
du projet :

```bash
python content-agents/skills/skill-creator/scripts/validate_skill.py \
  content-agents/skills/<nom>
```

Ce validateur contrôle le package avec le chargeur réel d’AMK et reconstruit
`content-agents/skills/index.json`. Corriger toutes les erreurs avant de livrer.
Tester aussi tout script ajouté avec un exemple représentatif.

Terminer par un bilan court : déclenchement, fichiers créés ou modifiés,
ressources ajoutées, outils demandés et résultat de validation.
