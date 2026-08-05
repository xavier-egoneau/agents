# Décisions de gouvernance

## Status

Journal historique de la version 0.1. La numérotation suit l’ordre de décision,
pas l’ordre du fichier; une entrée marquée « remplacée » reste lisible mais ne
décrit plus le contrat courant. Les contrats transverses les plus récents sont
les décisions 27 à 30.

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
que sur la machine d’origine. Rien n’est détruit; cette politique a ensuite été
retirée par la décision 26 et aucune commande d’adoption n’est requise.

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

### 28. Sandbox Codex réutilisé, politique AMK conservée
Le Guardian et le sandbox répondent à deux questions distinctes : le premier
décide si une action est autorisée, le second borne techniquement ce qu'un
processus autorisé peut atteindre. Une approbation ne désactive jamais le
sandbox.

Lorsqu'il est installé et capable d'appliquer tout le profil, le helper de Codex
CLI devient le backend OS d'AMK. `safe` étend `:read-only`; `limited` et `power`
étendent `:workspace`. Le runtime et les artefacts de session sont les seules
racines ajoutées en écriture, les fichiers providers/secrets et les dossiers de
credentials sont interdits, et le réseau est désactivé par défaut. Un appel de
processus doit le demander explicitement.

Ici, `:workspace` décrit d'abord la frontière d'écriture des processus. AMK
ajoute `:root = deny` et `:minimal = read` pour éviter la lecture générale du
poste, puis réouvre seulement l'interpréteur nécessaire. Le workspace du web
n'est plus implicitement la racine applicative : il est choisi par le run,
fourni avec `amk web -w`, repris du dépôt Git courant ou remplacé par un dossier
personnel sous `content-agents/workspaces/` si le lancement vient d'un CWD trop
large.

Le backend Windows `unelevated` refuse les exclusions de lecture fines. AMK ne
retire pas ces exclusions pour obtenir artificiellement un statut vert : seul
`elevated` est accepté. Si aucun backend complet n'est disponible, `safe` et
`limited` refusent l'exécution et `doctor` expose la cause. À terme, le helper
devra être livré avec AMK plutôt que dépendre d'une installation Codex voisine.
Sur Windows, le compte hors ligne bloque bien l'egress public mais pas le
loopback brut; cette limite reste annoncée et les URL locales sont contrôlées
par le Guardian.

### 29. `workspace: null` désigne l’espace personnel de l’agent
Une session détachée d’un projet ne s’exécute plus dans la racine de
l’application. Sa valeur logique reste `null` dans les événements, projections,
routines et API, mais le kernel la résout vers le dossier durable et visible
`content-agents/workspaces/<agent_id>/`. Le socle crée celui de `main`; ceux des
autres agents sont créés à la demande.

Cette séparation évite deux confusions. `null` continue de signifier « aucun
projet rattaché » et ne devient pas un chemin figé dans une routine, tandis que
les outils et le sandbox reçoivent toujours une frontière concrète. L’API expose
donc aussi `effective_workspace` et `workspace_kind`; l’interface peut ouvrir le
dossier réellement utilisé sans prétendre qu’il s’agit d’un projet.

Les profils `limited` et `power` restent bornés au workspace effectif. Pour une
session globale, cette frontière est donc l’espace personnel de son agent, pas
le dépôt AMK ni le dossier utilisateur.

### 30. Le workflow versionne le contrat de sécurité et le fuseau
Le catalogue remis au générateur de workflows comprend les paramètres de chemin
et d’URL interprétés par le Guardian. Ces métadonnées sont acceptées par le
schéma et participent au hash de base : une évolution de sécurité invalide donc
explicitement une proposition antérieure, sans empêcher d’en générer une
nouvelle. `workspace: null` est représenté par une chaîne vide uniquement dans
ce hash; l’exécution continue de le résoudre vers l’espace personnel.

Une échéance cron est calculée dans `execution.timezone` du workflow,
`Europe/Paris` par défaut. Le scheduler reconvertit chaque instant de référence
UTC vers ce fuseau avant de chercher l’occurrence suivante. La base IANA
`tzdata` est une dépendance runtime afin que le même contrat fonctionne sous
Windows et respecte les changements heure d’été/hiver.

Enfin, un outil calendrier peut recevoir `days_ahead` sans bornes ISO
pré-calculées. Un workflow n’a donc plus besoin d’inventer une commande shell
Unix pour exprimer « aujourd’hui et les sept jours suivants ».

### 31. Un provider llama.cpp peut être externe ou géré par AMK

Un provider `llama-cpp` qui déclare seulement une URL reste un endpoint local
externe. La présence de `models_dir` active au contraire un cycle de vie géré :
AMK valide le binaire et les GGUF, démarre `llama-server` à la première requête,
le conserve entre les runs et le redémarre si le modèle ou les arguments
changent. Un verrou par port sérialise les démarrages concurrents.

Les processus ne sont pas attachés au dépôt courant. Leur état et leurs logs
résident dans la racine utilisateur `content-agents/runtime/providers/`, tandis
que les chemins des modèles restent explicites dans `providers.json`. Chaque
provider possède son port et ne peut reprendre un port occupé par un processus
étranger. Le serveur est forcé sur `127.0.0.1` : être local ne signifie pas
l’exposer au réseau.

### 32. Les paramètres sont déclarés par module, pas par tool

Une configuration concerne généralement une intégration partagée par plusieurs
tools : les deux opérations CalDAV utilisent le même compte, tandis que seule
la fonction `image_inspect` du module Perception dépend du modèle Gemma. Le
manifest d’un module portant la capacité `config` décrit donc ses champs et les
tools concernés. La surface construit la page Paramètres en parcourant ces
manifests; les modules sans configuration n’y créent aucune fiche vide.

Le rendu générique accepte un ensemble fermé de types et le backend revalide
chaque valeur. Les paramètres ordinaires vivent dans
`content-agents/tool-settings.json`, par namespace de module. Les secrets ne
passent jamais par ce document : un champ `secret` référence un nom fixe dans
le coffre et l’API ne révèle que son état. `vision.json` reste une source
historique de moindre priorité afin que la migration ne casse pas les postes
déjà configurés.

### 33. Telegram est un canal d’agent avec une identité unique autorisée

Telegram n’est pas un tool appelé par le modèle : c’est une source de messages
qui déclenche le même kernel que la surface Web. Sa configuration appartient à
l’agent, tandis que le token du bot et l’identifiant utilisateur restent dans
le coffre sous des noms dérivés de l’id de l’agent. Le document `telegram.json`
ne contient que l’activation, la visibilité et l’offset de polling.

Une session est déterminée par le couple agent/chat, ce qui conserve le contexte
sans mélanger un échange privé et un groupe. Le contrôle d’accès vérifie
strictement `message.from.id` avant de créer un run : connaître le bot, appartenir
au même groupe ou envoyer depuis un autre bot ne donne aucun accès. L’option de
masquage devient une propriété projetée de la session et agit uniquement sur sa
présence dans les listes. Elle ne change pas son workspace : `null` continue de
désigner l’espace personnel par défaut de l’agent. Le catalogue de l’application
demande explicitement l’agrégation des canaux visibles avec les sessions du
projet courant; le filtre de workspace brut de l’API conserve sa sémantique.

Le long polling démarre avec l’API, sauvegarde son offset après chaque update et
ignore le backlog lors de la première activation. Les erreurs réseau sont
réessayées sans journaliser l’URL contenant le token.

Les approvals utilisent les primitives natives du Bot API : clavier inline,
`callback_query`, acquittement obligatoire par `answerCallbackQuery`, puis reprise
du batch durable du kernel. Le callback contient l’identité du run et une
empreinte du batch, reste sous la limite Telegram de 64 octets et devient caduc
dès que le batch change. `/clear` est une commande native qui purge toutes les
données de la session sans passer par le modèle.

Un consentement doit décrire ce qu’il accorde. `ApprovalRequest` conserve donc
la description de l’outil et les arguments expurgés examinés par le Guardian.
Telegram et le Web présentent l’action, la cible, les paramètres, le motif de
blocage, les risques et surtout la portée durable exacte
`(tool_name, action_family, path)`, valable jusqu’à la remise à zéro de la
conversation.

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

### 34. Une skill s’active par l’agent ou par sa commande, pas par une case

La modale Skills proposait des cases à cocher qui alimentaient
`RunRequest.skills`. Comme `agent_factory` additionne cette liste à celle de
l’agent avant de dédoublonner, cocher une skill que l’agent précharge déjà
n’avait aucun effet — mais l’affichait dans le composer, laissant croire que
c’était elle qui l’activait. Rien n’indiquait par ailleurs ce que l’agent
apportait : la modale montrait huit skills sans distinction.

Il reste donc deux voies, chacune avec sa trace. La **configuration de l’agent**
pour ce qui est permanent, écrit dans son markdown. La **commande slash**
déclarée sous `amk.commands` pour l’ajout ponctuel, visible dans le fil de
conversation. Une troisième voie, sans trace et sans durée, rendait un
comportement de session inexplicable trois jours plus tard.

Le composer affiche désormais l’état réel : les skills de l’agent en pastilles
neutres, sans croix — elles se changent dans sa configuration — et les skills
ajoutées par commande en pastilles d’accent, retirables. Conséquence assumée :
une skill sans commande déclarée ne peut plus être chargée ponctuellement.
