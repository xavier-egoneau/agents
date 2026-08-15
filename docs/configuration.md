# Configuration, providers et secrets

## Providers

Le registre se trouve dans `content-agents/providers.json`. Partir de
`providers.example.json`; `default_provider` doit correspondre à un `id` de la
liste.

Pour DeepSeek, laisser `api_key` vide et fournir de préférence la variable
d’environnement :

```powershell
$env:DEEPSEEK_API_KEY = "votre-clé"
uv run amk providers check --provider deepseek
```

La valeur écrite directement dans `providers.json` prime sur la variable. Ce
fichier est protégé localement, mais ne doit jamais être versionné ni copié.

Pour un provider `connection_type: auth`, utiliser son identifiant déclaré :

```powershell
uv run amk auth login codex
uv run amk auth status codex
uv run amk auth logout codex
```

Les jetons OAuth résident dans le trousseau du système. Vérifier les identifiants
effectifs avec `amk providers list`; ne pas confondre l’`id` local et le `kind`
de l’adaptateur.

### llama.cpp local

Un provider `llama-cpp` fonctionne de deux façons :

- avec `base_url` seulement, AMK utilise un serveur déjà administré ailleurs ;
- avec `models_dir`, AMK gère un `llama-server` persistant et le démarre au
  premier usage. Changer de modèle redémarre le serveur du provider.

Exemple minimal :

```json
{
  "id": "llama-local",
  "kind": "llama-cpp",
  "connection_type": "local",
  "model": "mon-modele-q4",
  "models_dir": "C:\\models\\gguf",
  "server_binary": "C:\\llama.cpp\\llama-server.exe",
  "port": 8123,
  "n_gpu_layers": 999,
  "num_ctx": 16384,
  "flash_attn": true,
  "preserve_thinking": true,
  "reasoning_budget": 16384,
  "num_predict": 32768,
  "startup_timeout_seconds": 240
}
```

`model` est le nom du fichier `.gguf` sans extension. Les modèles shardés sont
regroupés automatiquement. `llama_args` reçoit une liste d’arguments bruts,
un élément par token, par exemple `["--n-cpu-moe", "21"]`. Le serveur écoute
uniquement sur `127.0.0.1`; son état et ses logs vivent sous
`content-agents/runtime/providers/`.

`preserve_thinking` conserve le raisonnement des réponses précédentes dans les
tours suivants. `reasoning_budget` configure la limite llama.cpp (`-1` sans
limite, `0` désactivé) et `num_predict` limite l'ensemble de la sortie, réflexion
comprise. Le budget de raisonnement doit donc rester inférieur à `num_predict`.

```powershell
uv run amk providers check --provider llama-local
uv run amk providers status --provider llama-local
uv run amk providers start llama-local
uv run amk providers stop llama-local
```

`check` valide le binaire et le dossier sans charger le modèle. La page de
gestion des providers expose les mêmes réglages.

## Secrets d’outils

Dans la surface de conversation :

```text
/secret DATABASE_PASSWORD valeur
/secret_list
```

`/secret` est intercepté avant le modèle. La valeur n’entre ni dans le prompt,
ni dans les événements, ni dans les arguments tracés. Le modèle ne voit que le
nom opaque du secret. `content-agents/secrets.json` est exclu des tools
filesystem et protégé par permissions/DACL.

## Agents et skills

Les agents principaux vivent dans `content-agents/agents/*.md`. Le front matter
déclare notamment `id`, `provider`, `model`, `modules`, `skills` et `delegates`.

Les skills globales vivent dans `content-agents/skills/<nom>/SKILL.md`. Des
skills propres à un projet peuvent être placées sous les emplacements reconnus
dans son workspace, notamment `.agents/skills/` et `.codex/skills/`.

```powershell
uv run amk agents list
uv run amk agents validate
uv run amk skills list
uv run amk skills validate
```

## Paramètres des tools

La page **Paramètres** est construite automatiquement à partir des modules qui
déclarent la capacité `config`. Les modules sans paramètres restent visibles
dans le catalogue des tools, mais aucune fiche vide n’est ajoutée à cet écran.

Les valeurs ordinaires sont regroupées par module dans
`content-agents/tool-settings.json`. Les champs de type `secret` suivent un
circuit séparé : leur valeur est écrite dans `secrets.json`, n’est jamais
retournée par l’API et l’interface ne reçoit qu’un indicateur configuré/non
configuré.

Deux intégrations utilisent actuellement ce contrat :

- **Agenda iCloud** demande l’identifiant Apple et un mot de passe
  d’application pour `icloud_list_calendars` et `icloud_list_events` ;
- **Vision locale** expose le GGUF, la projection multimodale, le binaire
  `llama-server`, le port et les paramètres de chargement utilisés par
  `image_inspect`.

Un module déclare ses champs dans son `module.json`. Les types rendus par
l’interface sont volontairement bornés : texte, secret, fichier, dossier,
nombre, booléen, sélection et liste de chaînes. Le backend valide de nouveau le
schéma et les valeurs avant toute écriture.

## Canal Telegram d’un agent

Le formulaire d’un agent propose la case **Telegram**. Une fois activée, elle
demande l’identifiant numérique de l’unique utilisateur autorisé et le token du
bot fourni par BotFather. Ces deux valeurs sont des secrets write-only : elles
ne sont stockées ni dans le markdown de l’agent, ni dans `telegram.json`.

AMK utilise le long polling Telegram tant que la surface API fonctionne. Chaque
couple agent/chat possède une session durable. Le switch **Masquer cette session
dans l’application** retire uniquement cette conversation de la liste des
sessions. Sans projet explicite, son `workspace: null` est résolu normalement
vers `content-agents/workspaces/<agent_id>/`. Le catalogue de l’application
agrège explicitement les canaux Telegram visibles avec les sessions du projet
affiché, sans modifier le workspace auquel ces conversations sont rattachées.

Quand un tool requiert une autorisation, le bot affiche l’action demandée, la
fonction de l’outil, sa cible, ses paramètres exacts, la raison du blocage, ses
risques et la portée qui sera accordée, avec deux boutons **Refuser** et
**Autoriser**. Les arguments proviennent de l’appel réellement contrôlé par le
Guardian; les secrets sont expurgés avant leur stockage et leur affichage.
La portée est elle aussi explicite : même outil, même famille d’action et même
cible — ou toute cible équivalente lorsqu’aucune cible précise n’existe — dans
la conversation courante jusqu’à `/clear`.
Le clic répond à la callback Telegram, invalide immédiatement les boutons puis
reprend le run suspendu. Si la reprise produit une nouvelle demande, un nouveau
clavier est envoyé. Les anciens boutons sont liés à l’ensemble exact des
autorisations qu’ils représentent et ne peuvent donc pas valider une demande
ultérieure. Le contrôle de `message.from.id` s’applique aussi aux callbacks.
Les chats connus et l’empreinte du dernier clavier envoyé sont conservés dans
`telegram.json` afin de restaurer une demande après redémarrage sans la dupliquer.
Si une autorisation est encore en attente, un nouveau message renvoie le clavier
au lieu de lancer un run concurrent.

La commande native `/clear` supprime le contexte durable de la conversation
Telegram courante sans appeler le modèle. Elle nettoie également les approvals,
snapshots, plans et artefacts liés; le message suivant recrée la session avec le
même identifiant mais sans historique. AMK publie cette commande dans le menu du
bot avec `setMyCommands`.

Le filtrage porte sur `message.from.id`, pas seulement sur le chat. En message
privé comme dans un groupe ou supergroupe, tout message venant d’un autre
utilisateur — y compris un autre bot — est ignoré sans réponse et sans création
de session. Dans un groupe, les réponses du bot restent naturellement lisibles
par les membres du groupe. Le mode confidentialité configuré via BotFather peut
également limiter les messages de groupe transmis au bot aux commandes,
mentions et réponses.

Au premier démarrage d’un canal, AMK positionne son curseur sur la fin du backlog
Telegram afin de ne pas rejouer d’anciens messages. L’offset est ensuite conservé
dans `content-agents/telegram.json`. Désactiver Telegram arrête le polling mais
conserve les secrets pour une réactivation; supprimer l’agent supprime aussi ses
deux secrets Telegram.

L’API conserve un filtrage strict par défaut. Un client qui construit un
catalogue de conversations peut demander cette agrégation avec
`GET /api/sessions?workspace=...&include_channels=true`. Une session marquée
`hidden` reste exclue dans les deux cas.

## Ketch et recherche Web

`amk setup` cherche Ketch via `AMK_KETCH_BIN`, le `PATH`, `~/bin` et
`~/.local/bin`, puis installe un binaire géré si nécessaire. La recherche utilise
DuckDuckGo par défaut; `ketch config` configure les autres backends.

## Vision locale

`amk setup --full` prépare llama.cpp et Gemma. La configuration effective se
modifie depuis la page **Paramètres**, section **Vision locale**. L’ancien
`content-agents/vision.json` reste lu pour compatibilité; les valeurs enregistrées
par la nouvelle interface dans `tool-settings.json` ont priorité. Sans
surcharge, AMK utilise ses valeurs par défaut et télécharge le modèle configuré
au premier besoin. Le serveur écoute uniquement sur `127.0.0.1`.

## RAG

Sans `rag.json`, l’index déterministe `feature_hash` fonctionne sans réseau.
Pour des embeddings denses, copier `rag.example.json` vers `rag.json`, puis
renseigner un endpoint compatible OpenAI. `credential_ref` contient un nom de
secret, jamais sa valeur.
