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

## Le backend

AMK exécute les commandes dans un conteneur Linux jetable, via Docker. Le même
moteur sur les trois systèmes : le profil de sécurité s’écrit une fois et ne se
rejoue pas par plateforme.

Chaque commande part dans un conteneur `--rm`, sans aucune capacité
(`--cap-drop=ALL`), sans élévation possible (`--security-opt=no-new-privileges`),
réseau coupé sauf demande explicite du Guardian. Seuls le workspace, les
artefacts, `/tmp` et le cache sont montés — le reste de la machine est
invisible. En mode `safe`, le workspace est monté en lecture seule.

L’image par défaut est banale et remplaçable par `AMK_SANDBOX_IMAGE`. Le backend
se désactive avec `AMK_DOCKER_SANDBOX=0`.

Un client installé ne suffit pas : le démon doit répondre. Docker Desktop peut
être présent et arrêté, et prétendre à l’isolation dans ce cas ferait échouer
chaque commande au lieu de retomber proprement sur le régime des autorisations.

### Portée réelle

Sur Windows et macOS, Docker place déjà les conteneurs Linux dans une machine
virtuelle, ce qui ajoute une frontière au-dessus des namespaces. Sur Linux natif
le noyau est partagé : barrière solide contre un agent qui se trompe ou qu’on a
détourné par injection, pas contre un exploit noyau.

Sur Linux, appartenir au groupe `docker` équivaut à peu près à être root.
Préférer Docker rootless ou Podman, dont l’interface est compatible.

### Sans Docker

`macOS` retombe sur Seatbelt. Partout ailleurs, il n’y a plus d’isolation : le
Guardian réclame alors une autorisation pour chaque commande, et `safe` comme
`limited` refusent l’exécution native. `amk doctor` dit lequel des deux cas
s’applique.

## Approbations

```powershell
uv run amk approvals list
uv run amk approvals resolve <approval-id> --approve
```

Les décisions et actions sont écrites dans le JSONL de session. Une action
refusée n’est jamais exécutée; une action à confirmer suspend le run puis le
reprend après résolution.
