# Décisions de gouvernance

## Status

Décisions actives, révisées avec les schémas et comportements de la version 0.1.

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

### 13. Audit primaire, projection reconstruisible
Les JSONL append-only sont la source d’audit. SQLite est une projection rapide,
idempotente et supprimable. Les anciens événements restent lisibles; aucune
migration ne réécrit les journaux.

### 14. Cycle durable des runs
Un run suit `created → running → approval_pending → resuming → running →
terminal`. Une suspension n’émet jamais `session.completed`. Une approval reprend
le même `run_id` et le même `tool_call_id`.

### 15. Contrat uniforme des tools
Chaque tool renvoie `ToolResult` (`ok`, `data`, `error`, `metadata`). Le manifeste
est vérifié contre l’implémentation et le guardian valide chaque résultat avant
qu’il soit tracé ou rendu au modèle.

### 16. Parallélisation sous lease
Une étape parallélisable déclare des `write_scopes`. SQLite conserve son owner,
son run et l’expiration du lease. Deux périmètres imbriqués ou identiques ne
peuvent pas être actifs simultanément; le parent valide le résultat avant
`completed`.

### 17. Défense en profondeur
Le guardian décide, la sandbox réduit l’impact d’un contournement, et les
résolveurs réseau bloquent DNS, redirections et sous-requêtes privées. Les
valeurs de secrets connues sont masquées par valeur, pas seulement par nom de
champ.
