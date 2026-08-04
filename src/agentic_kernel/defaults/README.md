# Socle d'installation

Contenu de référence livré avec l'application et matérialisé dans
`content-agents/` au premier démarrage, ou par `amk init`.

## Pourquoi ce dossier existe

`content-agents/` est l'espace utilisateur : il n'est pas versionné, car il
contient des données et des secrets propres à chaque poste. Mais les skills de
base, le prompt système et l'agent `main` ne sont pas des données utilisateur —
ce sont des ressources de l'application. Sans socle versionné, la seule façon
d'installer AMK sur une seconde machine était de copier `content-agents/`, ce
qui transportait aussi l'état local : routines pointant vers des dossiers
inexistants, mémoires d'une autre machine, clés API.

## Ce qui est ici, et ce qui n'y est pas

| Dans le socle | Jamais dans le socle |
|---|---|
| `system.md`, `agents/main.md` | `secrets.json`, clés API |
| `skills/` de base | `state.db`, `sessions/` |
| `*.example.json` (gabarits) | `providers.json` renseigné |
| `whitelist_paths.json` vide | chemins d'une machine |

## Règles de matérialisation

- Un fichier existant n'est **jamais** écrasé : l'utilisateur reste maître de
  son espace.
- Les gabarits `*.example.json` sont copiés tels quels ; c'est à l'utilisateur
  de les renommer et de les renseigner.
- Une skill de base absente est restaurée, une skill modifiée est conservée.

## Faire évoluer les skills de base

Modifier ici, pas dans `content-agents/skills/`. Les postes existants ne
recevront pas automatiquement la mise à jour : `amk init` restaure ce qui
manque, sans toucher à ce qui existe.
