# AMK Web

Surface web du Agentic Markdown Kernel.

## Développement local

Depuis la racine du projet, lancer le backend :

Renseigner `api_key` dans l’entrée DeepSeek de `content-agents/providers.json`, puis :

```bash
uv run amk serve
```

Puis lancer cette surface :

```bash
cd surfaces/web
npm install
npm run dev
```

Le proxy serveur utilise `AMK_KERNEL_URL`, avec `http://127.0.0.1:8765` par défaut. Les secrets provider ne doivent jamais être déclarés dans les variables publiques du frontend.
