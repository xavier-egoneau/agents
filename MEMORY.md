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
- Une skill rattachée à un agent est annoncée par l'index et chargée par
  `load_skill` à l'usage. Son corps n'entre dans le prompt que si elle déclare
  `load: always`, si le run la demande explicitement, si la mémoire utilisateur
  l'a rendue implicite, ou si `load_skill` est hors de l'allowlist du run.
- Le débit réel d'un run local vient des compteurs `/metrics` de llama-server,
  relevés avant et après l'appel : `prefill_tokens_per_second` et
  `generation_tokens_per_second` rejoignent `usage.details`. Les providers
  distants n'en publient pas, et l'interface s'abstient alors d'afficher une
  vitesse plutôt que d'en déduire une du temps mur à mur.
- Guardian, sandbox d'exécution et politique réseau encadrent les outils
  sensibles; les valeurs de secrets connues sont masquées par valeur.
- Un résultat d'outil est borné à 8 000 caractères avant d'entrer dans le
  contexte : début, fin, taille réelle et chemin du résultat complet, écrit dans
  les artefacts de la session. Ce qui n'entre jamais dans la fenêtre n'a pas à
  en être retiré par la compaction.
- Un agent qui déclare des `delegates` ne peut écrire que dans le dossier de
  données du kernel — son espace personnel, sa bibliothèque, ses routines. Toute
  écriture ailleurs est refusée, avec le nom de ses enfants dans le message. La
  consigne équivalente, placée en tête du prompt, ne suffisait pas : l'agent
  produisait vingt-sept `patch` dans un projet sans jamais déléguer.
- Compaction du contexte à 70 % de la fenêtre connue, snapshot exact compressé
  écrit avant réduction.
- L'occupation avant conversation compte les instructions, les skills inscrites
  et le nom, la description et le schéma d'entrée des outils exposés à l'agent.
  Jamais `tools/index.json` en entier : ce catalogue interne ne part pas vers le
  modèle et le compter faisait dépasser le seuil de compaction dès le premier
  message, avec une cible de réduction négative donc inatteignable.
- Routines planifiées avec workflow optionnel, prévalidées avant activation.
  Leur cron reste évalué dans le fuseau du workflow après chaque occurrence;
  les métadonnées de sécurité Guardian font partie du contrat versionné.

## Installation

- `amk setup` prépare le socle, Ketch et les dépendances web; il réutilise les
  installations existantes. `--full` complète seulement les éléments llama.cpp
  ou Gemma absents, `--no-downloads` reste purement local.
- La racine applicative ne dépend plus du CWD. Un checkout existant garde son
  `content-agents/`; une installation neuve utilise la racine de données OS.
- Le workspace est une donnée explicite du run, pas la racine de l'application :
  `amk web -w` le fixe; hors dépôt Git, le fallback est un dossier neutre sous
  les données AMK plutôt que le dossier utilisateur. Une valeur logique `null`
  est conservée en base et résolue à l’exécution vers l’espace personnel durable
  `content-agents/workspaces/<agent_id>/`.
- Le manifeste `.amk-defaults.json` met à jour uniquement les fichiers livrés
  que l'utilisateur n'a pas modifiés.
- `amk doctor` expose le backend réellement applicable. AMK exécute les
  commandes dans un conteneur Docker jetable (`--cap-drop=ALL`,
  `no-new-privileges`, réseau coupé par défaut); sur macOS sans démon Docker,
  le backend Seatbelt prend le relais. Sans backend, `safe` et `limited`
  refusent l'exécution native et `power` exécute sans confinement, gardé par
  les seules heuristiques du Guardian.
- Le sandbox ne couvre que le module `process` : Ketch, `pdftotext`, le
  navigateur et `git` tournent sur l'hôte. Les ports publiés sont liés au
  loopback; sous Linux natif le conteneur tourne sous l'uid de l'utilisateur;
  `AMK_SANDBOX_MEMORY` relève le plafond mémoire. Une commande serveur sans
  `network` tranché reçoit le réseau automatiquement, et le Guardian demande
  alors confirmation dans tous les modes.
- Une routine dont le workspace n'existe pas sur cette machine reste visible et
  modifiable; elle est signalée au démarrage et par `amk crons list`.
- `.venv/` est l'environnement Windows du dépôt. Un `uv run` ou `uv sync` lancé
  depuis un autre système d'exploitation sur ce même dossier considère cet
  environnement comme invalide, vide `Lib/site-packages`, puis échoue à
  supprimer `Scripts/` — le dépôt reste alors sans dépendances et toute commande
  répond `ModuleNotFoundError: No module named 'agentic_kernel'`. Depuis un
  environnement Linux monté sur le dépôt, passer `UV_PROJECT_ENVIRONMENT` sur un
  chemin hors du dépôt. Réparation : `uv sync` relancé sous Windows.

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

## Documentation

- `README.md` est la porte d’entrée; les procédures utilisateur sont séparées
  sous `docs/` : installation, configuration, workspaces, sandbox, routines,
  dépannage et développement.
- `DECISION.md` reste le journal des raisons; une décision remplacée n’est pas
  une description du comportement courant.

## Dette connue

- Le RAG n'a jamais été exécuté : aucun document indexé à ce jour.
- La surface web distribuée dépend encore de Node/npm; elle n'est pas encore
  livrée comme un artefact runtime autonome dans le wheel Python.
- La distribution n'embarque pas de helper sandbox propre : elle dépend de
  Docker (ou de Seatbelt sur macOS). Sans backend complet, les modes
  safe/limited refusent l'exécution native.
- Un provider `llama-cpp` doté de `models_dir` est géré par AMK : démarrage au
  premier usage, réutilisation persistante, changement de GGUF par redémarrage,
  état et logs sous `content-agents/runtime/providers/`. Sans `models_dir`, il
  reste un simple endpoint OpenAI-compatible externe.
- La page Paramètres est générée depuis les modules déclarant la capacité
  `config`. Les valeurs ordinaires sont regroupées dans `tool-settings.json`;
  les secrets restent write-only dans `secrets.json`. CalDAV et la vision locale
  utilisent ce contrat, avec lecture rétrocompatible de `vision.json`.
- Un agent peut exposer un canal Telegram configuré depuis son formulaire. Le
  token et l’unique id utilisateur autorisé sont write-only; tout autre
  `message.from.id` est ignoré, y compris en groupe. Les sessions sont durables
  par agent/chat et peuvent être exclues de la liste sans modifier leur
  workspace effectif, celui de l’agent lorsque la valeur logique reste `null`.
  Les approvals sont résolus par boutons inline avec reprise du run et contrôle
  strict du même utilisateur; `/clear` purge nativement la session Telegram.
- `page.tsx` dépasse 3 000 lignes.
- `rag.py` utilise le chemin absolu du workspace comme identifiant de projet.
