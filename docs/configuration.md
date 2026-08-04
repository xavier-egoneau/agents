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

## Ketch et recherche Web

`amk setup` cherche Ketch via `AMK_KETCH_BIN`, le `PATH`, `~/bin` et
`~/.local/bin`, puis installe un binaire géré si nécessaire. La recherche utilise
DuckDuckGo par défaut; `ketch config` configure les autres backends.

## Vision locale

`amk setup --full` prépare llama.cpp et Gemma. La configuration effective vit
facultativement dans `content-agents/vision.json`. Sans ce fichier, AMK utilise
ses valeurs par défaut et télécharge le modèle configuré au premier besoin.
Créer un objet JSON seulement pour surcharger `model_path`, `hf_repo`,
`mmproj_path`, `server_binary`, `port` ou `llama_args`. Le serveur écoute
uniquement sur `127.0.0.1`.

## RAG

Sans `rag.json`, l’index déterministe `feature_hash` fonctionne sans réseau.
Pour des embeddings denses, copier `rag.example.json` vers `rag.json`, puis
renseigner un endpoint compatible OpenAI. `credential_ref` contient un nom de
secret, jamais sa valeur.
