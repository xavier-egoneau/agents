# Mémoire du projet

## État initial

- Le projet démarre avec une architecture orientée Markdown.
- Une base minimale de gouvernance a été établie.
- Le système est pensé autour d’un prompt système, d’un kernel et d’une modularité progressive.

## Décisions en cours

- Priorité donnée à la clarté et à la traçabilité.
- Choix d’un modèle de projet simple avant toute implémentation technique lourde.

## État du kernel

- Le package `agentic_kernel` expose `Kernel.run(RunRequest) -> RunResult`.
- Les agents Markdown, modules manifestés, journaux JSONL et budgets multi-agents sont implémentés.
- llama.cpp, DeepSeek, OpenAI Codex OAuth et Claude OAuth disposent d’adaptateurs réels.
- La CLI `amk` valide les agents et modules, contrôle les providers, gère OAuth et lance les sessions.
- Les agents et skills existants de Claude, Codex et du standard `.agents` sont découverts directement, avec chargement progressif des skills.
- L’agent principal utilise `deepseek-v4-flash`; la clé de `providers.json` est prioritaire, avec `DEEPSEEK_API_KEY` comme secours.
- Une première surface de chat existe sous `surfaces/web/`; elle utilise l’API `amk serve`, sélectionne agents et skills, puis affiche résultats, sessions et erreurs.

## Prochaines étapes

- Faire évoluer les modules réels à partir du module datetime de démonstration.
- Ajouter des agents spécialisés et leurs relations de délégation dans `content-agents/agents/`.
- Exécuter les tests d’intégration réseau avec les comptes et serveurs locaux disponibles.
