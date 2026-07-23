# Décisions de gouvernance

## Status

En cours de définition initiale.

## Décisions retenues

### 1. Markdown-first
Le système doit être décrit, configuré et documenté en Markdown autant que possible.

### 2. Kernel modulaire
Un kernel unique orchestre les interactions, mais reste indépendant des modules spécifiques.

### 3. Provider abstrait
Le kernel ne doit pas dépendre d’un provider particulier au départ ; une interface de connexion doit permettre d’ajouter facilement un fournisseur.

### 4. Mémoire explicite
La mémoire doit être stockée de manière lisible et traçable, avec un format simple adapté à l’évolution du système.

### 5. Gouvernance légère
Le projet s’appuie sur trois fichiers de base : README.md, DECISION.md et MEMORY.md, complétés par un espace utilisateur dédié.

### 6. Séparation utilisateur/app
L’espace utilisateur est isolé dans content-agents/ pour distinguer les données utilisateur des composants applicatifs.

### 7. Application modulaire
Le dossier tools/ représente l’application et doit contenir les composants logiciels et le kernel, sans mélanger les données utilisateur.

### 8. Kernel Python typé
Le kernel public est un package Python 3.12 fondé sur Pydantic AI. Pydantic AI Harness est épinglé et isolé derrière l’interface du kernel afin que ses évolutions ne contaminent pas les configurations ni les modules.

### 9. Agents hiérarchiques
Les agents sont déclarés en Markdown et organisés en graphe acyclique de superviseurs et d’enfants. Les délégations partagent un budget global mais conservent des historiques isolés.

### 10. Index dérivé
Chaque module possède son propre manifeste. `tools/index.json` est un artefact déterministe généré et vérifié, jamais la source primaire des métadonnées.

### 11. Secrets hors configuration
Les clés sont référencées par variables d’environnement et les identifiants OAuth sont conservés dans le trousseau du système.

### 12. Compatibilité des artefacts agents
Le kernel consomme directement les agents Markdown Claude et `.agents`, les agents TOML Codex et le format partagé `SKILL.md`. Il normalise les métadonnées en mémoire sans réécrire ni déplacer les artefacts d’origine.
