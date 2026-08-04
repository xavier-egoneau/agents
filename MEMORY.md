# Mémoire du projet

État courant du dépôt. Le journal des choix et de leurs raisons est dans
`DECISION.md` : ce fichier ne décrit que ce qui est vrai aujourd'hui.

## Ce que fait le kernel

- `agentic_kernel` expose `Kernel.run(RunRequest) -> RunResult`.
- Agents et skills en Markdown, modules manifestés dans `tools/modules/`,
  journal JSONL append-only, projections SQLite reconstructibles.
- Adaptateurs providers réels : llama.cpp, DeepSeek, OpenAI Codex OAuth,
  Claude OAuth. L'agent principal utilise `deepseek-v4-flash`, la clé de
  `providers.json` primant sur `DEEPSEEK_API_KEY`.
- Les agents et skills de Claude, Codex et du standard `.agents` sont découverts
  directement, avec chargement progressif des skills.
- Guardian, sandbox d'exécution et politique réseau encadrent les outils
  sensibles; les valeurs de secrets connues sont masquées par valeur.
- Compaction du contexte à 70 % de la fenêtre connue, snapshot exact compressé
  écrit avant réduction.
- Routines planifiées avec workflow optionnel, prévalidées avant activation.

## Installation

- `amk setup` prépare le socle, Ketch et les dépendances web; il réutilise les
  installations existantes. `--full` complète seulement les éléments llama.cpp
  ou Gemma absents, `--no-downloads` reste purement local.
- La racine applicative ne dépend plus du CWD. Un checkout existant garde son
  `content-agents/`; une installation neuve utilise la racine de données OS.
- Le manifeste `.amk-defaults.json` met à jour uniquement les fichiers livrés
  que l'utilisateur n'a pas modifiés.
- `amk doctor` expose notamment l'absence actuelle de sandbox filesystem sous
  Windows et Linux.
- Une routine dont le workspace n'existe pas sur cette machine reste visible et
  modifiable; elle est signalée au démarrage et par `amk crons list`.

## Mémoire

- Session : contexte et compaction, rien ne survit à la fermeture.
- Projet : `DECISION.md` et `MEMORY.md`, versionnés avec le dépôt.
- Bibliothèque : `content-agents/knowledge/library/`, markdown ingérés depuis
  des URL ou des documents déposés dans `incoming/`. Locale au poste, ouvrable
  dans Obsidian.
- Transverse : `content-agents/system.md` et les définitions d'agents.
- Recherche : `knowledge_index` puis `knowledge_search`, avec `scope` valant
  `project` ou `library`. Index reconstructible.

## Surface web

`surfaces/web/` — Next.js et React, servie par `amk web`. Shell à rail
d'icônes, panneau latéral et dock redimensionnables. Le CSS sépare un contrat de
tokens (`app/theme/tokens.css`), une structure fixe et des thèmes
interchangeables; `npm run check:contrast` vérifie les ratios WCAG et la
complétude des scopes de contexte.

## Dette connue

- Le RAG n'a jamais été exécuté : aucun document indexé à ce jour.
- La surface web distribuée dépend encore de Node/npm; elle n'est pas encore
  livrée comme un artefact runtime autonome dans le wheel Python.
- Windows et Linux n'ont pas encore de backend d'isolation filesystem; les
  modes safe/limited refusent donc l'exécution native.
- `page.tsx` dépasse 4 000 lignes.
- `rag.py` utilise le chemin absolu du workspace comme identifiant de projet.
- `api.py` construit un `Kernel` à l'import, ce qui rend les tests sensibles au
  répertoire courant.
