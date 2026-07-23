# Agentic Markdown Kernel

Ce projet pose les bases d’un système agentique très modulaire, orienté Markdown et pensé pour évoluer sans couplage fort.

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
- content-agents/providers.json : configuration future des providers.
- content-agents/whitelist_paths.json : liste des chemins autorisés.
- tools/ : espace applicatif, considéré comme partie de l’application.

## Boucle minimale

1. Charger le prompt système depuis content-agents/system.md.
2. Résoudre le provider de génération à utiliser via content-agents/providers.json.
3. Appeler le kernel pour orchestrer la logique.
4. Enregistrer les résultats et les observations dans MEMORY.md.

## Principes

- Markdown d’abord pour la lisibilité.
- Modularité forte : chaque composant doit pouvoir évoluer seul.
- Traceabilité : décisions et mémoire doivent rester visibles.
- Abstraction du provider : le kernel ne dépend pas d’un fournisseur précis.

## Démarrage

Le kernel nécessite Python 3.12 mais ne modifie pas le Python système. `uv` installe et verrouille l’environnement :

```bash
uv sync --extra dev --python 3.12
uv run amk modules check
uv run amk agents validate
uv run amk run --agent main "Quelle heure est-il ?"
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

Après le premier `npm install` dans `surfaces/web`, une seule commande lance les deux services :

```bash
uv run amk web
```

La commande arrête d'abord les anciennes instances AMK écoutant sur les ports 8765 et 3000, puis
lance l'API et la surface ensemble. Elle refuse de tuer un programme sans rapport qui utiliserait
l'un de ces ports. `Ctrl+C` arrête proprement les deux services.

Ouvrir ensuite `http://localhost:3000`. La surface utilise `AMK_KERNEL_URL=http://127.0.0.1:8765` par défaut; cette variable peut pointer vers un backend kernel déployé.

La section **Projets / CWD** permet d'ajouter un dossier par son chemin, de mémoriser les projets
récents dans le navigateur et de sélectionner le workspace envoyé à chaque run. Le backend valide
que le chemin existe et correspond à un dossier avant de l'accepter.

La CLI fournit également `amk providers list|check`, `amk auth login|logout|status` et `amk modules build-index|check`.

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
guardian est une politique d'autorisation, pas une sandbox du système d'exploitation.

## Agents, skills, modules et sessions

Le kernel charge sans conversion les agents Markdown de `content-agents/agents/`, `.agents/agents/` et `.claude/agents/`. Il accepte également les agents Codex natifs de `.codex/agents/*.toml`. Les champs Claude `name`, `description`, `model`, `tools`, `disallowedTools`, `permissionMode` et `skills` sont normalisés; les contraintes de tools importées restent des instructions et non une frontière de sécurité.

Les Agent Skills OpenAI et Claude sont découverts exclusivement sous `content-agents/skills/`. Leur format reste celui d’origine : `<nom>/SKILL.md` avec front matter `name` et `description`. Une skill existante doit donc être copiée ou installée explicitement dans ce dossier avant d’être exposée par AMK. Les dossiers voisins `scripts/`, `references/` et `assets/` ne sont ni déplacés ni modifiés. Un agent peut précharger un skill avec `skills: [...]`, la CLI avec `--skill`, ou le modèle peut appeler `load_skill` à partir du catalogue léger.

```bash
uv run amk skills list
uv run amk skills validate
uv run amk run --agent main --skill mon-skill "Exécute cette tâche"
```

Les modules exécutables vivent sous `tools/modules/<id>/`, chacun avec un `module.json`. `tools/index.json` est généré, déterministe et vérifié avant tout chargement.

Le module `web` expose une interface unique pour cinq surfaces de recherche : `search`, `scrape`,
`code`, `docs` et `crawl`. Il utilise le binaire stateless Ketch, force les sorties JSON, borne les
résultats et traduit ses codes d'erreur en catégories stables. Installation opérateur :

```bash
brew install 1broseidon/tap/ketch
ketch config
```

La recherche web utilise DuckDuckGo par défaut sans clé. Les autres backends et Context7 peuvent
être configurés directement dans Ketch. Les pages récupérées sont considérées comme des données non
fiables et les URL locales ou privées restent soumises au guardian, même en mode `power`.

Chaque exécution produit un journal append-only dans `content-agents/sessions/<session-id>.jsonl`. Le kernel impose par défaut une profondeur de 5, 24 exécutions d’agents, 6 exécutions concurrentes, 20 requêtes par agent et 30 minutes par session.

À chaque run et à chaque reprise, le kernel ajoute aux instructions un contexte d'exécution dynamique
avec la date, l'heure locale ISO, le fuseau horaire, le workspace/CWD actif et le niveau de sécurité.

## Providers et secrets

Trois connexions sont reconnues : `local`, `api_key` et `auth`. llama.cpp utilise son API compatible OpenAI, DeepSeek lit `DEEPSEEK_API_KEY`, et les connexions OpenAI Codex/Claude passent par `amk auth login <provider>`. Les jetons OAuth sont renouvelés automatiquement et conservés dans le trousseau du système, jamais dans le dépôt.

```bash
uv run amk auth login openai-codex
uv run amk auth login claude
```
