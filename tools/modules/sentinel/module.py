"""Sentinel — contrôle de sécurité ponctuel de la machine.

Trois partis pris structurent ce module.

**Le diff est le produit, pas la liste.** Une énumération complète des processus
et des persistances est illisible et ne sera relue par personne. Ce qui a de la
valeur est « quoi de neuf depuis le dernier passage » : chaque observation porte
une empreinte stable, comparée à une référence persistée. Sans cette référence,
le rapport quotidien serait identique chaque jour et cesserait d'être lu.

**Ce qui n'a pas pu être vérifié est dit.** Un scan qui se tait sur ses angles
morts laisse croire à une couverture qu'il n'a pas. Chaque section absente ou
dégradée — privilèges insuffisants, plateforme non couverte, binaire optionnel
manquant — apparaît explicitement dans le rapport.

**Rien n'est installé, rien n'est requis.** Le socle n'utilise que psutil et les
API du système. Les outils tiers (osquery, ReiKey, Dev Machine Guard) sont
détectés et exploités s'ils sont là, jamais exigés.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import psutil
from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

SECTIONS = ("processes", "connections", "persistence", "ai_environment", "input_hooks")

# Au-delà, la vérification de signature coûte plus que ce qu'elle rapporte : les
# exécutables non système d'une machine de développement se comptent en dizaines,
# pas en milliers. Un dépassement est signalé plutôt que tronqué en silence.
MAX_SIGNATURE_CHECKS = 120

# Un scan ne doit jamais devenir la cause du problème qu'il cherche. Toute sonde
# externe est bornée.
PROBE_TIMEOUT_SECONDS = 20.0


def _success(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": metadata}


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Classes écrites à la main plutôt que `@dataclass` : le registre charge les
# modules d'outils sans les inscrire dans `sys.modules`, et `dataclasses` a
# besoin d'y retrouver l'espace de noms du module pour résoudre ses annotations.
# Un décorateur ici rendrait le module inchargeable par l'application.
class Finding:
    """Une observation, identifiée de façon stable d'un scan à l'autre.

    L'empreinte ne porte que sur les éléments identitaires — pas sur le PID, pas
    sur l'horodatage : un même service redémarré doit garder son empreinte,
    sinon tout le parc paraîtrait neuf à chaque redémarrage.
    """

    def __init__(
        self,
        section: str,
        kind: str,
        label: str,
        identity: tuple[str, ...],
        severity: str = "info",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.section = section
        self.kind = kind
        self.label = label
        self.identity = identity
        self.severity = severity
        self.detail: dict[str, Any] = {} if detail is None else detail

    @property
    def fingerprint(self) -> str:
        payload = "\u0000".join((self.section, self.kind, *self.identity))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "kind": self.kind,
            "label": self.label,
            "severity": self.severity,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
        }


class SectionReport:
    """Le résultat d'une section, y compris son incapacité à conclure."""

    def __init__(
        self,
        name: str,
        findings: list[Finding] | None = None,
        limitations: list[str] | None = None,
    ) -> None:
        self.name = name
        self.findings: list[Finding] = [] if findings is None else findings
        self.limitations: list[str] = [] if limitations is None else limitations


# --------------------------------------------------------------------------
# État persistant : référence et acquittements
# --------------------------------------------------------------------------


def _state_path(ctx: RunContext[Any]) -> Path:
    """Fichier d'état dans le workspace de l'orchestrateur du run.

    Même règle que la bibliothèque de connaissance : l'état appartient à l'agent
    racine. Un sous-agent qui scanne pour le compte de `main` alimente la
    référence de `main`, sinon son acquittement se perdrait avec la délégation.
    """
    state_db = Path(ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db"))
    contenu = state_db.parent
    orchestrateur = str(getattr(ctx.deps, "orchestrator_id", "") or "")
    if not _AGENT_ID.fullmatch(orchestrateur):
        return contenu / "sentinel" / "state.json"
    return contenu / "workspaces" / orchestrateur / "sentinel" / "state.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "baseline": {}, "acknowledged": {}, "last_scan_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Un état illisible ne doit pas empêcher le scan : on repart d'une
        # référence vide, ce qui produit un rapport bruyant une fois, plutôt
        # qu'une routine cassée jusqu'à intervention manuelle.
        return {"schema_version": 1, "baseline": {}, "acknowledged": {}, "last_scan_at": None}
    for key, default in (("baseline", {}), ("acknowledged", {})):
        if not isinstance(data.get(key), dict):
            data[key] = default
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def classify(
    findings: Iterable[Finding],
    state: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Répartit les observations entre nouveauté, acquitté et déjà connu.

    L'acquittement prime sur la nouveauté : un écart validé une fois ne doit
    plus jamais remonter, sans quoi la routine finit par être ignorée en bloc —
    et c'est précisément le jour où elle avait raison.
    """
    baseline = state.get("baseline", {})
    acknowledged = state.get("acknowledged", {})
    resultat: dict[str, list[dict[str, Any]]] = {"new": [], "acknowledged": [], "known": []}
    for finding in findings:
        empreinte = finding.fingerprint
        if empreinte in acknowledged:
            resultat["acknowledged"].append(finding.to_dict())
        elif empreinte in baseline:
            resultat["known"].append(finding.to_dict())
        else:
            resultat["new"].append(finding.to_dict())
    return resultat


# --------------------------------------------------------------------------
# Sondes système
# --------------------------------------------------------------------------


def _run_probe(command: list[str]) -> str | None:
    """Exécute une sonde système bornée; `None` si elle n'aboutit pas.

    Une sonde absente ou en échec est une limitation à signaler, jamais une
    exception : le reste du scan garde sa valeur.
    """
    if shutil.which(command[0]) is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - commande fixe, sans entrée utilisateur
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _system_roots() -> tuple[Path, ...]:
    """Répertoires dont le contenu est présumé légitime.

    Les exclure n'est pas un blanc-seing : c'est admettre qu'un binaire système
    compromis relève de l'intégrité du système d'exploitation, hors de portée
    d'un scan en espace utilisateur.
    """
    if sys.platform == "win32":
        systeme = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        return (systeme, Path(r"C:\Program Files\WindowsApps"))
    if sys.platform == "darwin":
        return (Path("/System"), Path("/usr/bin"), Path("/usr/sbin"), Path("/usr/libexec"))
    return (Path("/usr/bin"), Path("/usr/sbin"), Path("/bin"), Path("/sbin"), Path("/usr/lib"))


def _is_system_path(candidate: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def collect_processes() -> SectionReport:
    """Processus en cours, hors binaires système.

    On ne remonte pas la liste complète : quelques centaines de lignes dont la
    quasi-totalité appartient au système noieraient le signal. Ce qui compte est
    ce qui tourne depuis un emplacement utilisateur.
    """
    rapport = SectionReport("processes")
    roots = _system_roots()
    refuses = 0
    for processus in psutil.process_iter(["pid", "name", "exe", "username", "cmdline"]):
        try:
            info = processus.info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            refuses += 1
            continue
        chemin = info.get("exe")
        if not chemin:
            refuses += 1
            continue
        executable = Path(chemin)
        if _is_system_path(executable, roots):
            continue
        rapport.findings.append(
            Finding(
                section="processes",
                kind="process",
                label=info.get("name") or executable.name,
                identity=(str(executable),),
                detail={
                    "executable": str(executable),
                    "username": info.get("username"),
                },
            )
        )
    if refuses:
        rapport.limitations.append(
            f"{refuses} processus non inspectables (privilèges insuffisants ou déjà terminés)"
        )
    return rapport


def _unsigned_windows(paths: list[Path]) -> tuple[list[str], list[str]]:
    """Sépare les exécutables sans signature Authenticode valide.

    Renvoie aussi les limitations : une absence de PowerShell ou un dépassement
    de volume doit se voir, sinon « aucun binaire non signé » se lirait comme un
    verdict alors que rien n'a été vérifié.
    """
    limitations: list[str] = []
    if not paths:
        return [], limitations
    if len(paths) > MAX_SIGNATURE_CHECKS:
        limitations.append(
            f"signatures vérifiées sur {MAX_SIGNATURE_CHECKS} exécutables sur {len(paths)}"
        )
        paths = paths[:MAX_SIGNATURE_CHECKS]
    encoded = json.dumps([str(item) for item in paths])
    script = (
        f"$paths = '{encoded}' | ConvertFrom-Json; "
        "$paths | ForEach-Object { "
        "  $s = Get-AuthenticodeSignature -LiteralPath $_ -ErrorAction SilentlyContinue; "
        "  [pscustomobject]@{ Path = $_; Status = if ($s) { $s.Status.ToString() } "
        "    else { 'Unknown' } } } | ConvertTo-Json -Compress"
    )
    sortie = _run_probe(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if sortie is None:
        limitations.append("signatures Authenticode non vérifiées (PowerShell indisponible)")
        return [], limitations
    try:
        charge = json.loads(sortie or "[]")
    except json.JSONDecodeError:
        limitations.append("signatures Authenticode illisibles")
        return [], limitations
    if isinstance(charge, dict):
        charge = [charge]
    non_signes = [
        str(item.get("Path"))
        for item in charge
        if isinstance(item, dict) and item.get("Status") != "Valid"
    ]
    return non_signes, limitations


def annotate_signatures(rapport: SectionReport) -> SectionReport:
    """Marque les processus non signés — le seul signal fort de cette section."""

    if sys.platform != "win32":
        rapport.limitations.append(
            f"signature des exécutables non vérifiée sur {platform.system()}"
        )
        return rapport
    chemins = sorted({Path(item.detail["executable"]) for item in rapport.findings})
    non_signes, limitations = _unsigned_windows(chemins)
    rapport.limitations.extend(limitations)
    suspects = set(non_signes)
    rapport.findings = [
        Finding(
            section=item.section,
            kind=item.kind,
            label=item.label,
            identity=item.identity,
            severity="warning" if item.detail["executable"] in suspects else "info",
            detail={**item.detail, "signed": item.detail["executable"] not in suspects},
        )
        for item in rapport.findings
    ]
    return rapport


def _is_private(address: str) -> bool:
    import ipaddress

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def collect_connections() -> SectionReport:
    """Connexions sortantes établies vers l'extérieur, et ports en écoute.

    C'est la couche qui compte le plus : un logiciel qui capture sans pouvoir
    transmettre est inoffensif. On ignore le trafic privé et la boucle locale,
    qui n'apprennent rien.
    """
    rapport = SectionReport("connections")
    try:
        connexions = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        rapport.limitations.append(
            "connexions réseau non listées (privilèges insuffisants) ; "
            "relancer en administrateur pour couvrir cette section"
        )
        return rapport
    noms: dict[int, str] = {}
    for connexion in connexions:
        if connexion.pid is None:
            continue
        if connexion.pid not in noms:
            try:
                noms[connexion.pid] = psutil.Process(connexion.pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                noms[connexion.pid] = "?"
        nom = noms[connexion.pid]
        if connexion.status == psutil.CONN_LISTEN and connexion.laddr:
            if str(connexion.laddr.ip) in {"0.0.0.0", "::"}:  # noqa: S104 - test, pas un bind
                rapport.findings.append(
                    Finding(
                        section="connections",
                        kind="listening",
                        label=f"{nom} écoute sur toutes les interfaces :{connexion.laddr.port}",
                        identity=(nom, str(connexion.laddr.port)),
                        severity="warning",
                        detail={"process": nom, "port": connexion.laddr.port},
                    )
                )
            continue
        if connexion.status != psutil.CONN_ESTABLISHED or not connexion.raddr:
            continue
        if _is_private(str(connexion.raddr.ip)):
            continue
        rapport.findings.append(
            Finding(
                section="connections",
                kind="outbound",
                label=f"{nom} → {connexion.raddr.ip}:{connexion.raddr.port}",
                identity=(nom, str(connexion.raddr.ip)),
                detail={
                    "process": nom,
                    "remote_address": str(connexion.raddr.ip),
                    "remote_port": connexion.raddr.port,
                },
            )
        )
    return rapport


# --------------------------------------------------------------------------
# Enrichissement optionnel
#
# Ces outils comblent des angles morts que le socle natif déclare lui-même. Ils
# sont détectés, jamais exigés : leur absence laisse la limitation en place, ce
# qui est la vérité de la couverture, plutôt que de faire échouer le scan.
# --------------------------------------------------------------------------

REIKEY_BINARY = Path("/Applications/ReiKey.app/Contents/MacOS/ReiKey")
DEV_MACHINE_GUARD = "stepsecurity-dev-machine-guard"


def _osquery_rows(query: str) -> list[dict[str, Any]] | None:
    sortie = _run_probe(["osqueryi", "--json", query])
    if sortie is None:
        return None
    try:
        lignes = json.loads(sortie or "[]")
    except json.JSONDecodeError:
        return None
    return [ligne for ligne in lignes if isinstance(ligne, dict)]


def enrich_persistence(rapport: SectionReport) -> SectionReport:
    """Couvre par osquery les persistances hors de portée du socle natif.

    Sur Windows, les tâches planifiées et les services sont des ancrages au
    moins aussi utilisés que les clés `Run`, et `winreg` n'y donne pas accès.
    """
    if sys.platform != "win32":
        return rapport
    manquant = "tâches planifiées et services non couverts par le socle natif"
    taches = _osquery_rows(
        "SELECT name, path, action FROM scheduled_tasks WHERE enabled = 1"
    )
    services = _osquery_rows(
        "SELECT name, path FROM services WHERE start_type != 'DISABLED'"
    )
    if taches is None or services is None:
        rapport.limitations.append(f"{manquant} ; installer osquery pour les couvrir")
        return rapport
    for ligne in taches:
        nom = str(ligne.get("name") or "")
        rapport.findings.append(
            Finding(
                section="persistence",
                kind="scheduled_task",
                label=f"Tâche planifiée · {nom}",
                identity=(nom, str(ligne.get("action") or "")),
                detail={"name": nom, "action": ligne.get("action"), "path": ligne.get("path")},
            )
        )
    for ligne in services:
        nom = str(ligne.get("name") or "")
        rapport.findings.append(
            Finding(
                section="persistence",
                kind="service",
                label=f"Service · {nom}",
                identity=(nom, str(ligne.get("path") or "")),
                detail={"name": nom, "path": ligne.get("path")},
            )
        )
    rapport.limitations = [item for item in rapport.limitations if item != manquant]
    return rapport


def enrich_input_hooks(rapport: SectionReport) -> SectionReport:
    """Délègue à ReiKey l'énumération des event taps sur macOS.

    C'est le seul point du scan où un outil tiers change la nature du résultat
    plutôt que son volume : sans lui, la section n'a rien à dire.
    """
    if sys.platform != "darwin" or not REIKEY_BINARY.exists():
        return rapport
    sortie = _run_probe([str(REIKEY_BINARY), "-scan", "-pretty"])
    if sortie is None:
        rapport.limitations.append("ReiKey présent mais son scan n'a pas abouti")
        return rapport
    try:
        charge = json.loads(sortie)
    except json.JSONDecodeError:
        rapport.limitations.append("ReiKey présent mais sa sortie n'a pas pu être lue")
        return rapport
    taps = charge.get("event taps") if isinstance(charge, dict) else charge
    if not isinstance(taps, list):
        rapport.limitations.append("ReiKey présent mais sa sortie n'a pas la forme attendue")
        return rapport
    for tap in taps:
        if not isinstance(tap, dict):
            continue
        processus = str(tap.get("process path") or tap.get("process") or "?")
        rapport.findings.append(
            Finding(
                section="input_hooks",
                kind="event_tap",
                label=f"Event tap · {Path(processus).name}",
                identity=(processus,),
                severity="warning",
                detail={"process": processus, "source": "reikey"},
            )
        )
    rapport.limitations = [
        item for item in rapport.limitations if "ReiKey" not in item
    ]
    return rapport


def enrich_ai_environment(rapport: SectionReport) -> SectionReport:
    """Signale Dev Machine Guard sans interpréter sa sortie.

    Son format n'a pas été vérifié ici : deviner un schéma produirait des écarts
    faux ou muets, ce qui est pire que l'absence. On note donc sa disponibilité,
    à charge de l'agent de le lancer explicitement si le besoin s'en fait
    sentir.
    """
    if shutil.which(DEV_MACHINE_GUARD) is None:
        return rapport
    rapport.limitations.append(
        f"{DEV_MACHINE_GUARD} est installé mais sa sortie n'est pas encore interprétée "
        "par Sentinel ; le lancer à part pour un inventaire détaillé"
    )
    return rapport


def _windows_persistence() -> Iterator[Finding]:
    import winreg  # disponible sur Windows uniquement

    emplacements = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU\\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM\\Run"),
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
            "HKCU\\RunOnce",
        ),
    )
    for ruche, sous_cle, etiquette in emplacements:
        try:
            with winreg.OpenKey(ruche, sous_cle) as cle:
                index = 0
                while True:
                    try:
                        nom, valeur, _ = winreg.EnumValue(cle, index)
                    except OSError:
                        break
                    index += 1
                    yield Finding(
                        section="persistence",
                        kind="registry_run",
                        label=f"{etiquette} · {nom}",
                        identity=(etiquette, nom, str(valeur)),
                        detail={"location": etiquette, "name": nom, "command": str(valeur)},
                    )
        except OSError:
            continue


def _plist_persistence() -> Iterator[Finding]:
    dossiers = (
        Path.home() / "Library" / "LaunchAgents",
        Path("/Library/LaunchAgents"),
        Path("/Library/LaunchDaemons"),
    )
    for dossier in dossiers:
        if not dossier.is_dir():
            continue
        for element in sorted(dossier.glob("*.plist")):
            yield Finding(
                section="persistence",
                kind="launch_item",
                label=f"{dossier.name} · {element.stem}",
                identity=(str(element),),
                detail={"path": str(element)},
            )


def _xdg_persistence() -> Iterator[Finding]:
    dossiers = (
        Path.home() / ".config" / "autostart",
        Path.home() / ".config" / "systemd" / "user",
    )
    for dossier in dossiers:
        if not dossier.is_dir():
            continue
        for element in sorted(dossier.iterdir()):
            if element.is_file():
                yield Finding(
                    section="persistence",
                    kind="autostart",
                    label=f"{dossier.name} · {element.name}",
                    identity=(str(element),),
                    detail={"path": str(element)},
                )


def collect_persistence() -> SectionReport:
    """Points d'ancrage au démarrage — là où un intrus doit se déclarer.

    Un logiciel qui veut survivre au redémarrage laisse forcément une trace ici.
    C'est le meilleur rapport signal/bruit du scan.
    """
    rapport = SectionReport("persistence")
    if sys.platform == "win32":
        rapport.findings.extend(_windows_persistence())
        rapport.limitations.append("tâches planifiées et services non couverts par le socle natif")
    elif sys.platform == "darwin":
        rapport.findings.extend(_plist_persistence())
    else:
        rapport.findings.extend(_xdg_persistence())
    demarrage = _startup_folders()
    for dossier in demarrage:
        for element in sorted(dossier.iterdir()):
            if element.is_file():
                rapport.findings.append(
                    Finding(
                        section="persistence",
                        kind="startup_folder",
                        label=f"Démarrage · {element.name}",
                        identity=(str(element),),
                        detail={"path": str(element)},
                    )
                )
    return rapport


def _startup_folders() -> list[Path]:
    if sys.platform != "win32":
        return []
    base = os.environ.get("APPDATA")
    if not base:
        return []
    dossier = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return [dossier] if dossier.is_dir() else []


def _mcp_configuration_files() -> list[Path]:
    """Emplacements connus des déclarations de serveurs MCP.

    Un serveur MCP est du code exécuté avec les droits de l'utilisateur et un
    accès direct au contexte de l'agent : son ajout mérite d'être vu.
    """
    maison = Path.home()
    candidats = [
        maison / ".claude.json",
        maison / ".codex" / "config.toml",
        maison / ".cursor" / "mcp.json",
        maison / ".config" / "Claude" / "claude_desktop_config.json",
        maison / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidats.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    return [item for item in candidats if item.is_file()]


def _declared_mcp_servers(document: Path) -> Iterator[str]:
    try:
        contenu = json.loads(document.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(contenu, dict):
        return
    serveurs = contenu.get("mcpServers")
    if isinstance(serveurs, dict):
        yield from (str(nom) for nom in serveurs)
    projets = contenu.get("projects")
    if isinstance(projets, dict):
        for reglage in projets.values():
            imbriques = reglage.get("mcpServers") if isinstance(reglage, dict) else None
            if isinstance(imbriques, dict):
                yield from (str(nom) for nom in imbriques)


def collect_ai_environment() -> SectionReport:
    """Serveurs MCP et extensions d'éditeur — la surface propre aux agents.

    Elle échappe à l'antivirus, qui ne voit qu'un interpréteur légitime lançant
    un script légitime. C'est pourtant le chemin le plus court vers le contexte
    de l'agent et les fichiers du projet.
    """
    rapport = SectionReport("ai_environment")
    documents = _mcp_configuration_files()
    if not documents:
        rapport.limitations.append("aucune configuration MCP connue trouvée")
    for document in documents:
        for nom in _declared_mcp_servers(document):
            rapport.findings.append(
                Finding(
                    section="ai_environment",
                    kind="mcp_server",
                    label=f"Serveur MCP · {nom}",
                    identity=(str(document), nom),
                    detail={"config": str(document), "server": nom},
                )
            )
    for dossier, etiquette in _extension_directories():
        for element in sorted(dossier.iterdir()):
            if element.is_dir():
                rapport.findings.append(
                    Finding(
                        section="ai_environment",
                        kind="ide_extension",
                        label=f"{etiquette} · {element.name}",
                        identity=(etiquette, element.name),
                        detail={"path": str(element)},
                    )
                )
    return rapport


def _extension_directories() -> list[tuple[Path, str]]:
    maison = Path.home()
    candidats = [
        (maison / ".vscode" / "extensions", "VS Code"),
        (maison / ".cursor" / "extensions", "Cursor"),
        (maison / ".vscode-server" / "extensions", "VS Code Server"),
    ]
    return [(chemin, etiquette) for chemin, etiquette in candidats if chemin.is_dir()]


def collect_input_hooks() -> SectionReport:
    """Capture des frappes clavier — la section la moins couvrable nativement.

    Sur macOS, l'essentiel passe par les event taps CoreGraphics, énumérables :
    ReiKey le fait bien, et rien en espace utilisateur Python ne l'égale.

    Sur Windows, il n'existe aucun point d'étranglement unique. `SetWindowsHookEx`
    n'expose pas ses hooks, et `GetAsyncKeyState` par sondage ne laisse aucune
    trace observable. Prétendre couvrir cette section serait mentir ; on déclare
    donc l'angle mort et on renvoie vers les couches qui, elles, agissent : un
    jeton matériel pour les secrets, et la surveillance des sorties réseau,
    couverte par la section `connections`.
    """
    rapport = SectionReport("input_hooks")
    if sys.platform == "darwin":
        rapport.limitations.append(
            "event taps CoreGraphics non énumérés par le socle natif ; installer ReiKey"
        )
    elif sys.platform == "win32":
        rapport.limitations.append(
            "aucune énumération des hooks clavier en espace utilisateur sur Windows ; "
            "s'appuyer sur la section connections et sur un jeton matériel pour les secrets"
        )
    else:
        rapport.limitations.append("détection de keylogger non couverte sur cette plateforme")
    return rapport


COLLECTORS = {
    "processes": lambda: annotate_signatures(collect_processes()),
    "connections": collect_connections,
    "persistence": lambda: enrich_persistence(collect_persistence()),
    "ai_environment": lambda: enrich_ai_environment(collect_ai_environment()),
    "input_hooks": lambda: enrich_input_hooks(collect_input_hooks()),
}


def scan_sections(sections: Iterable[str]) -> list[SectionReport]:
    rapports: list[SectionReport] = []
    for nom in sections:
        collecteur = COLLECTORS.get(nom)
        if collecteur is None:
            continue
        try:
            rapports.append(collecteur())
        except Exception as exc:  # une section en échec n'annule pas le scan
            rapports.append(
                SectionReport(nom, limitations=[f"section interrompue : {type(exc).__name__}"])
            )
    return rapports


# --------------------------------------------------------------------------
# Outils exposés à l'agent
# --------------------------------------------------------------------------


async def sentinel_scan(
    ctx: RunContext[Any],
    sections: Annotated[list[str] | None, Field(default=None)] = None,
    update_baseline: bool = True,
    justification: str = "",
) -> dict[str, Any]:
    """Scan de sécurité de la machine, restitué comme un écart à la référence."""
    demandees = [item for item in (sections or SECTIONS) if item in SECTIONS] or list(SECTIONS)
    rapports = scan_sections(demandees)
    chemin = _state_path(ctx)
    etat = load_state(chemin)
    observations = [finding for rapport in rapports for finding in rapport.findings]
    classees = classify(observations, etat)
    limitations = [
        {"section": rapport.name, "reason": raison}
        for rapport in rapports
        for raison in rapport.limitations
    ]
    premier_scan = not etat.get("baseline")
    if update_baseline:
        maintenant = _now()
        baseline = dict(etat.get("baseline", {}))
        for finding in observations:
            baseline.setdefault(finding.fingerprint, maintenant)
        etat["baseline"] = baseline
        etat["last_scan_at"] = maintenant
        save_state(chemin, etat)
    return _success(
        {
            "platform": platform.system(),
            "sections": demandees,
            # Un premier scan n'a rien à quoi se comparer : tout y est « nouveau »
            # sans que rien ne le soit. Le dire évite un rapport alarmant le
            # premier jour, puis muet les suivants.
            "baseline_established": premier_scan,
            "new": classees["new"],
            "acknowledged_count": len(classees["acknowledged"]),
            "known_count": len(classees["known"]),
            "limitations": limitations,
            "scanned_at": _now(),
        },
        state_path=str(chemin),
    )


async def sentinel_acknowledge(
    ctx: RunContext[Any],
    fingerprints: list[str],
    note: str = "",
    justification: str = "",
) -> dict[str, Any]:
    """Accepter des écarts pour qu'ils cessent de remonter dans les rapports."""
    chemin = _state_path(ctx)
    etat = load_state(chemin)
    acquittes = dict(etat.get("acknowledged", {}))
    maintenant = _now()
    retenus = [item.strip() for item in fingerprints if item.strip()]
    for empreinte in retenus:
        acquittes[empreinte] = {"note": note.strip()[:500], "at": maintenant}
    etat["acknowledged"] = acquittes
    save_state(chemin, etat)
    return _success(
        {"acknowledged": retenus, "total": len(acquittes)},
        state_path=str(chemin),
    )


class SentinelModule:
    def toolsets(self):
        return [FunctionToolset(tools=[sentinel_scan, sentinel_acknowledge])]

    def instructions(self):
        return [
            "Sentinel rend un écart, pas un inventaire : ne reformule pas la liste "
            "complète, ne rapporte que `new` et les limitations. Un scan sans "
            "nouveauté se conclut en une phrase.",
            "Ne qualifie jamais une observation de menace sur son seul caractère "
            "inédit : dis ce qui est apparu, ce que cela pourrait être, et ce qui "
            "reste à vérifier.",
            "Signale toujours les sections que le scan n'a pas pu couvrir ; les "
            "taire laisserait croire à une couverture qui n'existe pas.",
        ]

    def capabilities(self):
        return []


module = SentinelModule()
