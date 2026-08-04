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
Les identifiants OAuth sont conservés dans le trousseau du système. Les clés API
peuvent être référencées par variable d’environnement, par le store local
`secrets.json`, ou placées explicitement dans `providers.json`; ces deux fichiers
sont écrits en `0600`, protégés des tools et masqués dans les traces.

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

### 18. RAG explicite et vérifiable
Le corpus projet est découpé en chunks avec coordonnées de lignes, indexé dans
SQLite FTS5 et associé à des embeddings configurables. La recherche fusionne
lexical et vectoriel, mais le kernel n’injecte jamais automatiquement ses
résultats : l’agent reçoit des extraits citables et reste tenu de vérifier les
sources importantes. Les secrets, fichiers ignorés et journaux de session sont
exclus avant toute indexation.

### 19. Providers enregistrables
`ProviderFactory` sélectionne un `ProviderAdapter` via un registre explicite.
Les adaptateurs llama.cpp, API OpenAI-compatible, Codex OAuth et Claude OAuth
restent internes et remplaçables. Aucun type SDK provider ne traverse l’API
publique du kernel.

### 20. Contexte compact, audit intégral
La requête active est compactée à 70 % de la fenêtre connue et vise 50 % après
réduction. Les lectures reproductibles sont dédupliquées avant un résumé
sémantique structuré; les messages récents, mutations, approvals et preuves
importantes sont protégés. Un snapshot exact compressé est écrit avant la
compaction, et le JSONL reste intégralement reconstructible.

### 21. Socle livré, espace utilisateur préservé
`content-agents/` n’est pas versionné : il porte des données et des secrets
propres à chaque poste. Mais les skills de base, le prompt système et l’agent
`main` sont des ressources de l’application, livrées dans
`src/agentic_kernel/defaults/` et matérialisées par `amk init` ou au premier
démarrage. La matérialisation n’écrase jamais un fichier existant : elle
restaure ce qui manque et respecte ce qui a été adapté. La configuration est
proposée en gabarits `*.example.json`; créer un `providers.json` d’office
masquerait l’absence de clé.

Le socle porte désormais un manifeste d'empreintes. Un fichier livré resté
identique peut suivre une mise à jour; dès qu'il diverge, il devient une
variante utilisateur et n'est plus remplacé automatiquement.

### 27. Application, données utilisateur et workspace sont trois racines
Le CWD ne désigne plus l'installation AMK. Les assets sont résolus depuis le
package ou `AMK_APP_ROOT`, les données depuis la racine de plateforme ou
`AMK_HOME`, et le CWD n'est que le workspace par défaut. Un checkout existant
conserve son ancien `content-agents/` pour ne pas abandonner silencieusement
ses secrets et sessions.

Les outils compagnons sont gérés dans la racine de données : `amk setup`
installe Ketch, `--full` ajoute llama.cpp et prépare le modèle vision, avec
sélection de plateforme et vérification SHA-256. Les téléchargements lourds
restent explicites.

La politique Guardian ne déduit plus toutes les ressources de quelques noms
d'arguments codés en dur. Les manifestes peuvent déclarer leurs paramètres de
chemin et d'URL; cette couche reste la frontière principale pour les modules
Python exécutés dans le processus du kernel.

### 22. État local ancré à son installation
Un identifiant d’installation est stocké hors du dossier de contenu, dans le
répertoire de données de l’OS, hors de portée d’une copie ou d’une
synchronisation du projet. Les routines le portent, et ne sont ni listées ni
exécutées ailleurs : leurs chemins, autorisations et workspaces n’ont de sens
que sur la machine d’origine. Rien n’est détruit; `amk crons adopt` permet une
reprise explicite.

### 26. Les routines ne sont plus mises en quarantaine *(remplace 22)*
L'ancrage à l'installation est retiré des routines. Il protégeait d'un mélange
de bases dont il ne reste aucun canal réel : `content-agents/` est exclu de Git,
`amk init` supprime la raison de le copier, et aucune synchronisation ne porte
le dossier. Il masquait en revanche des routines sans le dire — le signalement
n'existait qu'en CLI, jamais dans l'interface où elles disparaissaient.

Ce qu'il fallait détecter reste détecté : une routine dont le workspace
n'existe pas ici est signalée au démarrage et par `amk crons list`, et échoue à
l'exécution avec un message qui nomme le chemin. La colonne `install_id` est
conservée pour ne pas casser une base existante; plus rien ne la lit.

Leçon à retenir au-delà de ce cas : une mise en quarantaine invisible est pire
que pas de quarantaine. Filtrer sans le montrer à l'endroit où l'utilisateur
regarde revient à supprimer en silence.

### 25. Bibliothèque de documents, en markdown lisible
La connaissance transverse au poste — documents déposés, pages archivées — vit
dans `content-agents/knowledge/` : `incoming/` reçoit les dépôts manuels,
`library/` les markdown convertis. Elle est distincte de la connaissance projet
(décision 24), qui appartient au dépôt et voyage avec lui.

Dossier plat et frontmatter YAML plutôt qu'une arborescence thématique : un
document relève souvent de plusieurs sujets, une hiérarchie force un choix
unique et le corriger casse les liens. Les tags sont contraints par le
vocabulaire de `library/_tags.md`; un tag inconnu est écarté et signalé, sinon
la taxonomie dérive au fil des ingestions. `library/` s'ouvre tel quel dans
Obsidian.

Les URL passent par `web_scrape`, qui extrait déjà le contenu lisible et
recharge les rendus JavaScript : l'ingestion n'émet aucune requête propre. Les
fichiers locaux sont convertis avec la bibliothèque standard et `pdftotext`,
sans dépendance nouvelle. Un format non pris en charge est refusé avec la liste
de ceux qui le sont, et le fichier déposé reste intact.

`scope` distingue les deux corpus à l'indexation comme à la recherche : sans
lui, ni l'agent ni le lecteur de ses citations ne saurait lequel a répondu.

### 23. Mémoire indexée, jamais devinée *(remplacée par 24)*
Les mémoires explicites étaient indexées en FTS5 avec le tokenizer du RAG et
classées par bm25. Cette table n’existe plus : voir la décision 24.

### 24. La connaissance d’un projet vit en markdown, dans le projet
Trois décisions prises le même jour — capture automatique du résumé de
compaction, injection des mémoires au démarrage, index FTS5 sur une table
`memories` — sont annulées, et le stockage SQLite correspondant est supprimé.

Ce qu’il fallait retenir d’un projet vit désormais dans `DECISION.md`,
`MEMORY.md` et `notes/`, à la racine du workspace. Trois raisons :

- **Inspectable.** Une base SQLite n’est lisible par personne sans outil
  dédié; on ne peut ni vérifier ce qui a été retenu, ni le corriger, ni le
  relire en diff.
- **Cohérent.** Les agents, les skills et le prompt système sont déjà des
  fichiers markdown. Une table était l’anomalie.
- **Au bon endroit.** La connaissance appartient au dépôt et doit voyager avec
  lui — à l’inverse de l’état d’exécution, ancré à l’installation (décision 22).
  Confondre les deux menait à des ancrages contradictoires.

Aucune capture automatique, aucune injection : une session démarre à vide. Elle
dispose déjà de sa propre mémoire — le contexte et sa compaction. Ce qui doit
survivre est écrit explicitement dans les fichiers, sur demande ou selon la
consigne portée par la skill `dev`. `knowledge_index` et `knowledge_search`
restent le moyen de retrouver une information dans un corpus devenu volumineux.
