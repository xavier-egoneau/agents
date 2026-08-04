# Guardian et sandbox

Le Guardian et le sandbox ont des responsabilités différentes :

- le Guardian décide si une action est autorisée, refusée ou soumise à
  approbation;
- le sandbox borne techniquement le processus après cette décision.

Une approbation n’enlève jamais le sandbox.

## Modes

| Mode | Profil de processus | Usage |
|---|---|---|
| `safe` | lecture seule | inspection prudente |
| `limited` | workspace | travail courant, mode par défaut |
| `power` | workspace, politique Guardian plus permissive | actions explicitement assumées |

Le runtime et les artefacts de session reçoivent les accès nécessaires. Les
providers, secrets et dossiers de credentials restent interdits. Le réseau des
processus est désactivé par défaut; un outil doit le déclarer explicitement.

## Diagnostic

```powershell
uv run amk doctor
```

La ligne `sandbox` est l’autorité : ne pas déduire la sécurité du seul nom du
système. Sans backend capable d’appliquer tout le profil, `safe` et `limited`
refusent l’exécution native plutôt que de continuer silencieusement.

## Windows

AMK réutilise le helper officiel de Codex CLI. Le backend `unelevated` ne sait
pas appliquer les exclusions de lecture exigées; AMK requiert `elevated`.

```powershell
uv run amk sandbox setup
uv run amk doctor
```

Une confirmation administrateur Windows peut apparaître. Le compte sandbox hors
ligne bloque l’egress public, mais un socket loopback brut reste techniquement
possible; le Guardian contrôle donc toujours les destinations locales et
privées.

## macOS et Linux

Avec un Codex CLI compatible, AMK réutilise son helper OS. Sur macOS, le backend
Seatbelt historique reste un repli. Sur Linux, l’isolation effective dépend du
helper disponible. Toujours vérifier `amk doctor` sur la machine cible.

## Approbations

```powershell
uv run amk approvals list
uv run amk approvals resolve <approval-id> --approve
```

Les décisions et actions sont écrites dans le JSONL de session. Une action
refusée n’est jamais exécutée; une action à confirmer suspend le run puis le
reprend après résolution.
