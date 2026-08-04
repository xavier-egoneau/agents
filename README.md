# Agentic Markdown Kernel

AMK est un kernel agentique local, modulaire et multi-agent, orienté Markdown.
Il fournit une CLI, une API et une surface web autour de contrats stables pour
les agents, providers, tools, sessions, approvals et automatisations.

## Vision

L’objectif est de construire une base minimale où :
- un prompt système est décrit en Markdown,
- un kernel orchestre les interactions,
- les modules peuvent être ajoutés indépendamment,
- la mémoire et les décisions restent lisibles et traçables.

## Structure proposée

- README.md : vue d’ensemble du projet et guide d’orientation.
- DECISION.md : décisions architecturales et gouvernance.
- MEMORY.md : état courant, choix retenus et prochaines pistes.
- content-agents/ : espace utilisateur, contenant les données et préférences utilisateur.
- content-agents/system.md : prompt système de base, considéré comme une variable utilisateur.
- content-agents/skills/ : compétences ou modules définis côté utilisateur.
- content-agents/providers.json : registre des providers et références de connexion.
- content-agents/state.db : projection SQLite reconstructible des sessions, plans et crons.
- tools/ : espace applicatif, considéré comme partie de l’application.

## Boucle minimale

1. Charger le prompt système depuis content-agents/system.md.
2. Résoudre le provider de génération à utiliser via content-agents/providers.json.
3. Appeler le kernel pour orchestrer la logique.
4. Persister les événements dans le JSONL append-only et mettre à jour les projections SQLite.

## Principes

- Markdown d’abord pour la lisibilité.
- Modularité forte : chaque composant doit pouvoir évoluer seul.
- Traceabilité : décisions et mémoire doivent rester visibles.
- Abstraction du provider : le kernel ne dépend pas d’un fournisseur précis.

## Démarrage

Le kernel nécessite Python 3.12 mais ne modifie pas le Python système. `uv` installe et verrouille l’environnement :

```bash
uv sync --extra dev --python 3.12
uv run amk setup
cp content-agents/providers.example.json content-agents/providers.json
uv run amk modules check
uv run amk agents validate
uv run amk run --agent main "Quelle heure est-il ?"
```

`amk setup` installe l’espace utilisateur, Ketch et les dépendances web. Il
réutilise les binaires déjà présents dans le `PATH`, `~/bin` ou `~/.local/bin`.
Le profil `amk setup --full` réutilise également llama.cpp et le modèle vision
configuré lorsqu’ils existent, sinon il les installe. `amk setup --no-downloads`
ne matérialise que le socle. `amk doctor`
diagnostique ensuite les chemins, providers, outils, catalogue et capacités du
sandbox.

La surface accepte `amk web --workspace <projet>`. Sans option, le CWD n'est
retenu que s'il appartient à un dépôt Git; un lancement depuis le dossier
utilisateur emploie sinon l’espace personnel de l’agent
`content-agents/workspaces/main/` jusqu'à ce qu'un projet soit choisi dans
l'interface. Plus généralement, une session dont `workspace` vaut `null` reste
logiquement détachée d’un projet, mais s’exécute dans
`content-agents/workspaces/<agent_id>/`. La racine applicative, les données AMK
et le workspace effectif d'un run sont donc trois notions distinctes.

`amk init` matérialise seulement `content-agents/` : prompt système, agent
`main`, skills de base et gabarits de configuration. Cet espace n’est pas
versionné — il contient des données et des secrets propres à chaque poste — mais
son contenu de référence est livré avec l’application, dans
`src/agentic_kernel/defaults/`. Ne jamais copier `content-agents/` d’une machine
à l’autre : le dossier transporte aussi l’état local, c’est-à-dire des routines
pointant vers des dossiers inexistants et des clés API.

L’application et le workspace sont distincts : AMK retrouve ses assets depuis
son installation, tandis que le CWD reste seulement le workspace par défaut.
Un checkout existant conserve son `content-agents/`; une installation neuve
utilise le répertoire de données de la plateforme. `AMK_APP_ROOT` et `AMK_HOME`
permettent de surcharger explicitement ces deux racines.

L’initialisation est également déclenchée au démarrage de `amk serve` et
`amk web`. Un manifeste d’empreintes met à jour les fichiers du socle restés
inchangés, restaure ceux qui manquent et ne remplace jamais une variante
personnalisée.

Si un `content-agents/` a déjà été copié depuis un autre poste, ses routines
pointent vers des dossiers qui n’existent pas ici. Elles restent utilisables,
et le démarrage comme `amk crons list` signalent le chemin manquant :

```bash
uv run amk crons list
```

L’agent principal utilise DeepSeek par défaut. La clé peut être placée dans l’entrée `deepseek` de `content-agents/providers.json` :

```json
"api_key": "sk-..."
```

La variable d’environnement est utilisée seulement lorsque `api_key` est vide :

```bash
export DEEPSEEK_API_KEY="..."
uv run amk providers check --provider deepseek
```

## Surface web

La surface web vit dans `surfaces/web/` et communique avec le kernel par HTTP. La clé provider reste exclusivement dans l’environnement du processus Python.

`amk setup` exécute `npm ci` lorsque les dépendances de développement sont
absentes. Une seule commande lance ensuite les deux services, même depuis un
autre dossier :

```bash
uv run amk web
```

La commande arrête d'abord toutes les autres instances lancées avec `amk web`, même depuis un
autre dossier ou sur d'autres ports, puis lance l'API et la surface ensemble. Cette exclusivité
appartient uniquement à la surface web : un `amk serve` lancé seul n'est jamais arrêté. Si le port
web 3000 appartient à une autre application, AMK choisit le prochain port libre et affiche l'URL
retenue. Un conflit sur le port API reste une erreur explicite. `Ctrl+C` arrête proprement les deux
services.

Ouvrir l'URL `AMK Web` affichée au démarrage. La surface utilise
`AMK_KERNEL_URL=http://127.0.0.1:8765` par défaut; cette variable peut pointer vers un backend
kernel déployé.

La section **Projets / CWD** permet d'ajouter un dossier par son chemin, de mémoriser les projets
récents dans le navigateur et de sélectionner le workspace envoyé à chaque run. Le backend valide
que le chemin existe et correspond à un dossier avant de l'accepter.

La CLI fournit également `amk providers list|check`, `amk auth login|logout|status` et `amk modules build-index|check`.

### Secrets locaux

Les commandes RPPL natives `/secret` et `/secret_list` sont interceptées par le
kernel avant tout appel au modèle :

```text
/secret DATABASE_PASSWORD monSuperMotDePasse
/secret_list
```

La première enregistre la paire dans `content-agents/secrets.json` avec des
permissions `0600`. Le composer masque immédiatement la valeur et le kernel ne
l’ajoute ni au prompt modèle, ni à l’historique JSONL, ni aux traces. La seconde
retourne uniquement les noms disponibles. `secrets.json` est également protégé
contre la lecture et la recherche par les tools filesystem. Les noms sont
présentés au modèle comme références opaques; `http_request` peut par exemple
résoudre une référence via son argument `credential_env` sans placer sa valeur
dans les arguments ou les traces.

## Guardian et outils filesystem

Chaque run possède un workspace et un niveau `safe`, `limited` ou `power` (`limited` par défaut).
Tous les appels de modules passent par le guardian déterministe avant exécution. Une action refusée
n'est jamais exécutée; une action à confirmer suspend le run et peut être reprise depuis la CLI ou
la surface web.

```bash
uv run amk run --security-mode limited --workspace . "Mets à jour README.md"
uv run amk approvals list
uv run amk approvals resolve <approval-id> --approve
```

Le module `filesystem` fournit `read`, `list`, `write` et `delete`. `delete` déplace toujours la cible
dans `content-agents/sessions/trash/<session-id>/` après confirmation. Les outils actifs de
`tools/index.json` sont exposés par défaut; `content-agents/agents/<id>.tools-disabled.json` peut en
retirer pour un agent avec `{ "tools": ["delete"] }`.

Les décisions et phases d'exécution sont enregistrées dans le JSONL de session. Ces traces sont
séparées des observations bornées renvoyées au modèle et leurs champs secrets sont masqués. Le
guardian reste la politique d’autorisation. `command_run` et `process_start`
ajoutent une isolation d’exécution via un backend propre à la plateforme. Sur
une machine équipée de Codex CLI, AMK réutilise son helper OS : profil
`:read-only` en mode `safe`, profil `:workspace` en `limited` et `power`, secrets
explicitement interdits et réseau coupé sauf demande explicite. Il bénéficie
ainsi de Seatbelt sur macOS, de `bwrap`/`seccomp` sur Linux et du sandbox natif
sur Windows. Le backend Windows `unelevated` ne sait pas garantir les exclusions
de lecture exigées par AMK et est donc refusé; le mode `elevated` est requis.
`amk sandbox setup` déclenche le flux officiel d'installation élevée de Codex.
Sans helper Codex compatible, le backend Seatbelt historique reste disponible
sur macOS. Ailleurs, `safe` et `limited` refusent l’exécution; `power` exige une
approbation explicite. Les capacités sont interrogées par le Guardian plutôt
que déduites du nom de l’OS.
Les manifestes déclarent en plus quels arguments sont des chemins ou des URL :
le Guardian ne dépend donc plus uniquement de noms conventionnels comme
`path` ou `url`. Les modules Python restent privilégiés tant qu’ils s’exécutent
dans le processus du kernel; une isolation forte de tous les tools demandera
un hôte de modules séparé.

Sous Windows, l'utilisateur sandbox hors ligne bloque l'egress public, mais le
loopback reste techniquement joignable par un socket brut. Le Guardian continue
donc d'exiger une approbation pour les destinations locales ou privées; le
diagnostic ne présente pas cette combinaison comme une isolation réseau totale.

Les groupes de processus POSIX sont utilisés sur macOS et Linux. Windows utilise
un groupe de processus natif et une terminaison récursive contrôlée. Les fichiers
sensibles utilisent `0600` sur POSIX et une DACL limitée à l’utilisateur et à
`SYSTEM` sous Windows. Le sélecteur de workspace est disponible via AppleScript
sur macOS et le dialogue Tk natif sous Windows et Linux lorsqu’il est installé.

## Agents, skills, modules et sessions

Le kernel charge sans conversion les agents Markdown de `content-agents/agents/`, `.agents/agents/` et `.claude/agents/`. Il accepte également les agents Codex natifs de `.codex/agents/*.toml`. Les champs Claude `name`, `description`, `model`, `tools`, `disallowedTools`, `permissionMode` et `skills` sont normalisés; les contraintes de tools importées restent des instructions et non une frontière de sécurité.

Les Agent Skills OpenAI et Claude sont découverts exclusivement sous `content-agents/skills/`. Leur format reste celui d’origine : `<nom>/SKILL.md` avec front matter `name` et `description`. Une skill existante doit donc être copiée ou installée explicitement dans ce dossier avant d’être exposée par AMK. Les dossiers voisins `scripts/`, `references/` et `assets/` ne sont ni déplacés ni modifiés. Un agent peut précharger un skill avec `skills: [...]`, la CLI avec `--skill`, ou le modèle peut appeler `load_skill` à partir du catalogue léger.

```bash
uv run amk skills list
uv run amk skills validate
uv run amk run --agent main --skill mon-skill "Exécute cette tâche"
```

Les modules exécutables vivent sous `tools/modules/<id>/`, chacun avec un `module.json`. `tools/index.json` est généré, déterministe et vérifié avant tout chargement.
Les 74 tools actuels partagent le contrat `ToolResult`
(`ok`, `data`, `error`, `metadata`). `amk modules build-index` et
`amk modules check` vérifient automatiquement la correspondance exacte entre
manifestes et implémentations, les schémas, les timeouts et les doublons.

### Plans et parallélisation

Les commandes `/plan` et `/build` utilisent un plan normalisé dans SQLite. Une
étape parallélisable doit déclarer au moins un `write_scope`. Avant de lancer un
subagent, le kernel réserve l’étape avec un lease durable; deux scopes identiques
ou imbriqués ne peuvent pas être actifs en même temps. Un lease expiré redevient
récupérable après un crash.

Le subagent ne termine jamais directement son étape : son résultat passe à
`validating`. Le parent doit vérifier les fichiers, artefacts ou tests puis
appeler le tool autonome `plan_validate` avec une liste de preuves. Sans preuve,
l’état `completed` est refusé.

Le kernel injecte à chaque run une carte légère et bornée du workspace : langages,
gestionnaires de paquets, points d’entrée, documentation, tests et commandes connues.
Elle respecte `.gitignore`, exclut les secrets et sert uniquement à orienter
l’exploration; l’agent doit toujours lire les sources ciblées avant d’agir.

Pour l’exploration sémantique, le module `codegraph` expose des tools autonomes pour
la recherche de symboles, les appelants, les dépendances et l’analyse d’impact. La
commande RPPL `/explore <question>` charge la skill correspondante, synchronise le
cache technique local `.codegraph` si nécessaire et retombe sur la recherche
filesystem lorsque CodeGraph ne couvre pas le projet.

```bash
npm install -g @colbymchenry/codegraph
uv run amk modules check
```

AMK détecte également une installation sans droits administrateur dans
`~/.local/bin/codegraph`, ou le chemin explicite fourni par `AMK_CODEGRAPH_BIN`.

### Vision locale transparente

Les images jointes suivent automatiquement les capacités du provider actif :

- un provider déclaré avec `"vision": true` reçoit directement l’image ;
- avec un modèle textuel, l’image devient un artefact local, `image_inspect`
  l’analyse avec Gemma 4 E2B Q4 via `llama.cpp`, puis seule l’observation
  textuelle bornée est envoyée au modèle principal.

Le bouton image reste donc disponible avec DeepSeek. Le premier appel télécharge
le modèle GGUF dans le cache Hugging Face local; les appels suivants réutilisent
le modèle et le serveur loopback. `llama-server` n’écoute que sur
`127.0.0.1:8081`. Les réglages peuvent être surchargés en copiant
`vision.example.json` vers `content-agents/vision.json`.

Le tool autonome `image_inspect(path, question, detail)` est également disponible
pour analyser une image existante ou un screenshot, avec `detail` égal à
`fast`, `balanced` ou `precise`.

### RAG local hybride

Le module `memory` sépare la mémoire explicite du corpus documentaire. Le tool
`knowledge_index` découpe les fichiers en chunks chevauchants avec coordonnées
de lignes, calcule leurs hashes et embeddings, alimente SQLite FTS5 et retire
les entrées correspondant aux fichiers supprimés. Les fichiers inchangés ne
sont pas recalculés.

`knowledge_search` fusionne les classements lexical et vectoriel par reciprocal
rank fusion, applique un reranking de couverture, puis renvoie des extraits
bornés et des citations comme `src/kernel.py#L120-L164`. Aucun résultat RAG
n’est injecté automatiquement : l’agent appelle explicitement le tool et doit
vérifier les sources importantes avant de conclure.

Sans configuration, le backend local `feature_hash` fournit un index
déterministe sans téléchargement ni réseau. Pour une similarité sémantique
dense, copier `content-agents/rag.example.json` vers
`content-agents/rag.json` et renseigner un endpoint d’embeddings compatible
OpenAI, par exemple un serveur llama.cpp local. Une référence de secret peut
être fournie dans `credential_ref`; sa valeur n’entre ni dans les arguments du
tool ni dans les traces.

Le module `web` expose une interface unique pour cinq surfaces de recherche : `search`, `scrape`,
`code`, `docs` et `crawl`. Il utilise le binaire stateless Ketch, force les sorties JSON, borne les
résultats et traduit ses codes d'erreur en catégories stables. Installation opérateur :

`amk setup` réutilise d’abord Ketch depuis `AMK_KETCH_BIN`, le `PATH`, `~/bin` ou
`~/.local/bin`. À défaut, il télécharge le binaire précompilé correspondant à la
plateforme, vérifie son SHA-256 et le conserve dans le répertoire de données AMK
sans modifier le `PATH` système. `ketch config` reste disponible pour choisir les backends.

La recherche web utilise DuckDuckGo par défaut sans clé. Les autres backends et Context7 peuvent
être configurés directement dans Ketch. Les pages récupérées sont considérées comme des données non
fiables et les URL locales ou privées restent soumises au guardian, même en mode `power`.

Chaque exécution produit un journal append-only dans
`content-agents/sessions/<session-id>.jsonl`. SQLite (`content-agents/state.db`)
en est une projection reconstructible utilisée pour paginer sessions, messages,
événements, transitions et état de contexte. Les snapshots volumineux sont
compressés, adressés par hash et écrits avant l’événement qui les référence.
Le kernel impose par défaut une profondeur de 5, 24 exécutions d’agents,
6 exécutions concurrentes, 100 requêtes modèle par agent et 30 minutes par session.

À chaque run et à chaque reprise, le kernel ajoute aux instructions un contexte d'exécution dynamique
avec la date, l'heure locale ISO, le fuseau horaire, le workspace/CWD actif et le niveau de sécurité.

### Automatisations et reprise

Le scheduler de cronjobs appartient au kernel : il reste actif tant que
`amk web` ou `amk serve` tourne, recharge son état depuis SQLite et envoie
chaque occurrence dans la boucle normale du kernel. Chaque routine conserve
une seule session durable afin de ne pas encombrer l’historique.

Le panel **Automatisations** permet de créer, éditer, suspendre, tester, lancer
et supprimer les routines. L’utilisateur choisit une fréquence lisible
(minutes, heures, jour, semaine ou année); le kernel conserve sa représentation
cron interne et l’évalue dans le fuseau local du serveur. Un arrêt
transitoire peut être repris automatiquement à l’occurrence suivante; les
échecs de configuration ou d’authentification et les demandes d’approbation
restent bloqués pour intervention humaine.

Une session `failed`, `timeout`, `partial` ou `cancelled` peut aussi être
reprise depuis l’historique. La reprise crée un nouveau run dans la même
session et reconstruit son contexte depuis les snapshots et traces persistés.

## Providers et secrets

Trois connexions sont reconnues : `local`, `api_key` et `auth`. llama.cpp utilise son API compatible OpenAI, DeepSeek lit `DEEPSEEK_API_KEY`, et les connexions OpenAI Codex/Claude passent par `amk auth login <provider>`. Les jetons OAuth sont renouvelés automatiquement et conservés dans le trousseau du système, jamais dans le dépôt.

Les implémentations passent toutes par le protocole interne `ProviderAdapter`.
Les types OpenAI, Anthropic et Pydantic AI ne font pas partie de l’API publique
du kernel. `ProviderAdapterRegistry` permet d’enregistrer ou de remplacer un
adaptateur sans modifier `Kernel` ni `ProviderFactory`.

```bash
uv run amk auth login openai-codex
uv run amk auth login claude
```

## Validation

La commande suivante exécute Ruff, tous les tests Python, ESLint, le build de la
surface et les parcours Playwright. Les tests réseau réels restent opt-in.

```bash
./scripts/check.sh
```

Sous Windows PowerShell :

```powershell
uv sync --extra dev --python 3.12
uv run playwright install chromium
npm ci --prefix surfaces/web
./scripts/check.ps1
```

La CI exécute les validations sur Ubuntu, macOS et Windows. Une sandbox absente
ou incapable d'appliquer le profil complet n’est jamais assimilée à une
isolation réussie : le backend natif non isolé reste réservé au mode `power`
avec approbation.
