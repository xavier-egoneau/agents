from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from .models import (
    ApprovalRequest,
    Event,
    GuardianDecision,
    GuardianVerdict,
    SecurityMode,
    ToolResult,
    ToolRisk,
)
from .network_policy import network_scope
from .platform.sandbox import sandbox_capabilities
from .trace_context import event_run_id

PROTECTED_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".git", ".codex"}
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "providers.json",
    "secrets.json",
}
SECRET_KEYS = {"api_key", "token", "password", "secret", "authorization"}


def guardian_parameters_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the parameters schema actually exposed through ``GuardianToolset``."""

    guarded = dict(schema)
    properties = dict(guarded.get("properties", {}))
    properties["justification"] = {
        "type": "string",
        "description": "Why this tool call is necessary for the user's request.",
        "minLength": 1,
    }
    required = list(dict.fromkeys([*guarded.get("required", []), "justification"]))
    guarded.update(properties=properties, required=required)
    return guarded


def canonical_path(raw: Any, workspace: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    if os.name == "nt" and _invalid_windows_path(raw):
        return None
    candidate = Path(raw).expanduser()
    return (workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def review_tool_call(  # noqa: C901 - dette: moteur de décision multi-critères
    *,
    tool_name: str,
    tool_call_id: str,
    agent_id: str,
    arguments: dict[str, Any],
    risks: list[ToolRisk],
    mode: SecurityMode,
    workspace: Path,
    trusted_read_roots: tuple[Path, ...] = (),
    personal_workspace: Path | None = None,
    workflow_grants: frozenset[tuple[str, str]] = frozenset(),
    path_parameters: tuple[str, ...] = (),
    url_parameters: tuple[str, ...] = (),
    delegates: tuple[str, ...] = (),
    delegated_write_exemptions: tuple[Path, ...] = (),
) -> GuardianDecision:
    justification = arguments.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        return GuardianDecision(
            verdict=GuardianVerdict.DENY,
            reason="A non-empty justification is required.",
            tool_name=tool_name,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            risks=risks,
            justification="",
            security_mode=mode,
        )
    path_keys = tuple(dict.fromkeys(("path", "cwd", "destination", *path_parameters)))
    url_keys = tuple(dict.fromkeys(("url", "seed", *url_parameters)))
    # A parameter may deliberately accept either a path or an URL (for example
    # knowledge_ingest.source). Once it is a valid network target it must not
    # also be interpreted as a local filesystem path.
    raw_paths = [
        arguments[key]
        for key in path_keys
        if arguments.get(key) is not None
        and not (key in url_keys and network_scope(arguments.get(key)) is not None)
    ]
    paths = [canonical_path(raw, workspace) for raw in raw_paths]
    path = paths[0] if paths else None
    network_path = next(
        (
            network_scope(value)
            for key in url_keys
            if isinstance((value := arguments.get(key)), str) and network_scope(value) is not None
        ),
        None,
    )
    if any(candidate is None for candidate in paths):
        verdict, reason = GuardianVerdict.DENY, "The path is invalid or ambiguous."
    elif any(_contains_symlink(raw, workspace) for raw in raw_paths):
        verdict, reason = GuardianVerdict.DENY, "Paths traversing symbolic links are denied."
    elif any(
        candidate is not None
        and (
            {part.casefold() for part in candidate.parts} & PROTECTED_PARTS
            or candidate.name.casefold() in SENSITIVE_NAMES
        )
        for candidate in paths
    ):
        verdict, reason = GuardianVerdict.DENY, "Protected or secret paths are denied."
    elif ToolRisk.SECRET in risks or ToolRisk.SYSTEM in risks:
        verdict, reason = GuardianVerdict.DENY, "Secret and system actions are denied."
    elif (
        hors := _hors_du_perimetre_d_orchestrateur(
            paths, risks, delegates, delegated_write_exemptions
        )
    ) is not None:
        verdict, reason = GuardianVerdict.DENY, hors
    elif (portee := _network_scope(arguments, url_keys)) == "private":
        verdict, reason = (
            GuardianVerdict.ASK,
            "Une cible du réseau local désigne une autre machine : confirmation requise.",
        )
    elif portee == "loopback" and mode is not SecurityMode.POWER:
        verdict, reason = (
            GuardianVerdict.ASK,
            "Une cible locale demande confirmation hors du mode power.",
        )
    elif (
        paths
        and ToolRisk.READ in risks
        and not any(risk is not ToolRisk.READ for risk in risks)
        and all(
            candidate is not None
            and any(_inside(candidate, root.resolve()) for root in trusted_read_roots)
            for candidate in paths
        )
    ):
        verdict, reason = (
            GuardianVerdict.ALLOW,
            "Reading a kernel-owned artifact for this session is allowed.",
        )
    elif any(
        candidate is not None and not _inside(candidate, workspace.resolve()) for candidate in paths
    ):
        verdict, reason = GuardianVerdict.ASK, "Actions outside the workspace require approval."
    elif ToolRisk.DESTRUCTIVE in risks:
        verdict, reason = GuardianVerdict.ASK, "Destructive actions always require approval."
    elif ToolRisk.SCREEN in risks:
        verdict, reason = GuardianVerdict.ASK, "Screen capture always requires approval."
    elif ToolRisk.EXECUTE in risks:
        verdict, reason = _review_execution(arguments, mode)
    elif (ToolRisk.NETWORK in risks or ToolRisk.EXTERNAL in risks) and str(
        arguments.get("method", "GET")
    ).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        verdict, reason = GuardianVerdict.ASK, "Mutating HTTP requests require approval."
    elif ToolRisk.WRITE in risks and mode is SecurityMode.SAFE:
        verdict, reason = GuardianVerdict.ASK, "Writes require approval in safe mode."
    elif (
        ToolRisk.WRITE in risks
        and mode is SecurityMode.LIMITED
        and any(candidate is not None and candidate.exists() for candidate in paths)
        # Le rangement personnel de l'agent — USER.md, DECISIONS.md, sa
        # bibliothèque, l'état de Sentinel — n'est pas le travail de
        # l'utilisateur. Y réécrire un fichier est le fonctionnement normal, et
        # demander à chaque fois entraîne à valider sans lire : le jour où la
        # demande porte sur quelque chose qui compte, elle passe avec les autres.
        and not (
            personal_workspace is not None
            and all(
                candidate is not None and _inside(candidate, personal_workspace.resolve())
                for candidate in paths
            )
        )
    ):
        verdict = GuardianVerdict.ASK
        reason = "Overwriting an existing path requires approval in limited mode."
    elif ToolRisk.NETWORK in risks or ToolRisk.EXTERNAL in risks:
        if mode is SecurityMode.POWER:
            verdict, reason = GuardianVerdict.ALLOW, "External action allowed in power mode."
        else:
            verdict, reason = GuardianVerdict.ASK, "External actions require approval."
    else:
        verdict, reason = GuardianVerdict.ALLOW, "Action allowed by the active security mode."
    if verdict is GuardianVerdict.ASK and _covered_by_workflow(
        tool_name, arguments, workflow_grants
    ):
        # Le contrat a été lu et accepté avant d'être enregistré : ce qu'il
        # déclare est autorisé. Redemander appel par appel ferait valider deux
        # fois la même chose, et surtout interromprait une routine qui s'exécute
        # sans personne devant l'écran.
        #
        # Un refus, lui, reste un refus : la concession relève ce qui aurait été
        # demandé, jamais ce qui est interdit.
        verdict = GuardianVerdict.ALLOW
        reason = "Déclaré par le workflow accepté de la routine."
    return GuardianDecision(
        verdict=verdict,
        reason=reason,
        tool_name=tool_name,
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        path=str(path) if path else network_path,
        risks=risks,
        justification=justification.strip(),
        security_mode=mode,
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _network_scope(arguments: dict[str, Any], url_keys: tuple[str, ...]) -> str:
    """Classe une cible réseau : `public`, `loopback` ou `private`.

    La distinction compte. `localhost` désigne cette machine — celle où l'agent
    exécute déjà des commandes et lit déjà des fichiers : lui interdire d'ouvrir
    le serveur qu'il vient de lancer n'ajoute aucune protection, seulement une
    confirmation de plus.

    Une adresse privée non locale désigne en revanche **une autre machine** :
    routeur, NAS, imprimante, service interne. Là, la confirmation garde son
    sens, y compris en `power`.
    """
    portee = "public"
    for key in url_keys:
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        hostname = urlparse(value).hostname
        if not hostname:
            continue
        minuscule = hostname.lower()
        if minuscule == "localhost":
            portee = "private" if portee == "private" else "loopback"
            continue
        if minuscule.endswith(".local"):
            return "private"
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            continue
        if address.is_loopback:
            portee = "private" if portee == "private" else "loopback"
        elif address.is_private or address.is_link_local or address.is_reserved:
            return "private"
    return portee


def _covered_by_workflow(
    tool_name: str,
    arguments: dict[str, Any],
    grants: frozenset[tuple[str, str]],
) -> bool:
    """Vrai quand l'appel correspond à ce que le contrat accepté déclare.

    Des arguments figés vides valent pour tout appel de l'outil : c'est la forme
    que produit le générateur, et l'`allowlist` du workflow restreint déjà
    l'agent à ces seuls outils.
    """
    if not grants:
        return False
    concessions = {figes for outil, figes in grants if outil == tool_name}
    if not concessions:
        return False
    if "" in concessions:
        return True
    fournis = {key: value for key, value in arguments.items() if key != "justification"}
    return any(
        all(fournis.get(key) == value for key, value in json.loads(figes).items())
        for figes in concessions
    )


def _contains_symlink(raw: Any, workspace: Path) -> bool:
    if not isinstance(raw, str):
        return False
    source = Path(raw).expanduser()
    if source.is_absolute():
        current = Path(source.anchor)
        parts = source.parts[1:]
    else:
        current = workspace
        parts = source.parts
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
        if not current.exists():
            break
    return False


def _invalid_windows_path(raw: str) -> bool:
    normalized = raw.strip().replace("/", "\\")
    lowered = normalized.casefold()
    if lowered.startswith(("\\\\?\\", "\\\\.\\")):
        return True
    without_drive = normalized[2:] if len(normalized) >= 2 and normalized[1] == ":" else normalized
    if ":" in without_drive:
        return True
    reserved = {"con", "prn", "aux", "nul", "clock$"}
    reserved.update(f"com{number}" for number in range(1, 10))
    reserved.update(f"lpt{number}" for number in range(1, 10))
    return any(
        part.rstrip(" .").split(".", 1)[0].casefold() in reserved
        for part in without_drive.split("\\")
        if part
    )


def action_family(risks: list[ToolRisk]) -> str:
    for risk in (
        ToolRisk.DESTRUCTIVE,
        ToolRisk.EXECUTE,
        ToolRisk.WRITE,
        ToolRisk.READ,
        ToolRisk.NETWORK,
    ):
        if risk in risks:
            return risk.value
    return "other"


def _hors_du_perimetre_d_orchestrateur(
    paths: list[Path | None],
    risks: list[ToolRisk],
    delegates: tuple[str, ...],
    exemptions: tuple[Path, ...],
) -> str | None:
    """Refuse à un orchestrateur d'écrire là où un de ses enfants doit écrire.

    Un orchestrateur à qui l'on demande par consigne de ne pas coder code quand
    même : sur une session mesurée, avec « Écrire un fichier de code toi-même
    est une erreur » en tête de son prompt, il a produit quatorze `patch` et
    deux `write` sans jamais appeler `agent_delegate`. Une instruction ne
    contraint rien; le périmètre d'écriture, si.

    Restent ouverts son espace personnel et le dossier de données du kernel :
    c'est là que vivent sa mémoire, sa bibliothèque, ses notes et ses routines,
    et rien de tout cela ne se délègue. Le reste appartient aux enfants.

    Un agent sans enfant n'est pas concerné : lui refuser l'écriture ne
    laisserait personne pour la faire.
    """
    if not delegates:
        return None
    if not any(risk in {ToolRisk.WRITE, ToolRisk.DESTRUCTIVE} for risk in risks):
        return None
    autorises = [root.resolve() for root in exemptions]
    dehors = [
        candidate
        for candidate in paths
        if candidate is not None and not any(_inside(candidate, root) for root in autorises)
    ]
    if not dehors:
        return None
    return (
        f"Cet agent délègue l'écriture hors de son espace personnel. "
        f"Confie la modification de {dehors[0]} à l'un de ses agents : "
        f"{', '.join(delegates)} — via `agent_delegate`, en lui donnant le chemin, "
        f"la contrainte et le critère de réussite."
    )


def _review_execution(arguments: dict[str, Any], mode: SecurityMode) -> tuple[GuardianVerdict, str]:
    program = str(arguments.get("program", "")).strip()
    executable = Path(program).name.casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
            break
    # `get(clé, défaut)` ne protège que de la clé absente. Un modèle qui envoie
    # explicitement `"args": null` — ce que fait couramment un appel à `dir` ou
    # `ls` sans argument — passait alors `None` à la boucle, et le run entier
    # tombait sur un `TypeError` avant même que le Guardian ait rendu son avis.
    args = [str(value) for value in arguments.get("args") or []]
    lowered = [executable, *(value.casefold() for value in args)]
    command = " ".join(lowered)
    destructive = (
        executable in {"rm", "rmdir", "shred", "mkfs"}
        or "reset --hard" in command
        or "clean -fd" in command
        or "--force" in lowered
    )
    installs = (
        executable in {"pip", "pip3", "brew"}
        and any(value in {"install", "uninstall"} for value in lowered)
    ) or (
        executable in {"npm", "pnpm", "yarn", "bun"}
        and any(value in {"install", "add", "remove", "uninstall"} for value in lowered)
    )
    network_program = executable in {"curl", "wget", "ssh", "scp", "nc", "ncat", "telnet"}
    network_requested = arguments.get("network") is True
    interpreter_escape = executable in {
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "php",
        "powershell",
        "pwsh",
        "cmd",
    }
    external_path_argument = any(
        value.startswith(("/", "~/", "../")) or "/../" in value for value in args
    )
    sandbox_available = sandbox_capabilities().execution_isolated
    if destructive:
        return GuardianVerdict.ASK, "Potentially destructive command requires approval."
    if mode is SecurityMode.SAFE:
        return GuardianVerdict.ASK, "Command execution requires approval in safe mode."
    if mode is SecurityMode.LIMITED and (
        installs
        or network_program
        or network_requested
        or interpreter_escape
        or external_path_argument
    ):
        return (
            GuardianVerdict.ASK,
            "Install, network, interpreter, or external-path commands require approval.",
        )
    if not sandbox_available:
        return (
            GuardianVerdict.ASK,
            "The execution sandbox is unavailable; explicit approval is required.",
        )
    known = {
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "node",
        "python",
        "python3",
        "pytest",
        "git",
        "make",
        "cmake",
        "cargo",
        "go",
        "ruff",
        "eslint",
        "tsc",
        "vite",
    }
    if mode is SecurityMode.LIMITED and executable not in known:
        return GuardianVerdict.ASK, "Unknown executable requires approval in limited mode."
    return GuardianVerdict.ALLOW, "Command execution allowed by the active security mode."


def approval_scope(decision: GuardianDecision) -> str:
    payload = {
        "tool": decision.tool_name,
        "family": action_family(decision.risks),
        "path": decision.path,
        "session": None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def redact(value: Any, key: str = "") -> Any:
    if any(secret in key.lower() for secret in SECRET_KEYS):
        return "***"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


# Ce qu'un résultat d'outil peut occuper dans le contexte, en caractères.
# Environ 2 300 tokens avec l'estimateur du kernel.
#
# Un `read` sur un fichier de 40 ko versait 11 000 tokens dans la fenêtre, que
# la compaction ne pouvait plus reprendre : sur une session mesurée le 10 août,
# l'historique atteignait 130 000 tokens pour une fenêtre de 65 536, avec douze
# compactions d'affilée sans effet. Le contenu intégral reste écrit à côté et
# reste lisible; seule la part montrée au modèle est bornée.
BUDGET_SORTIE_OUTIL = 8_000


def _tronquer(texte: str, budget: int) -> str:
    """Garde le début et la fin, coupe le milieu, dit combien manque.

    La fin compte autant que le début : les erreurs de compilation, les totaux
    et les conclusions s'y trouvent. Une troncature par la fin seule les perd.
    """
    if len(texte) <= budget:
        return texte
    tete = texte[: budget * 3 // 4]
    queue = texte[-(budget // 4) :]
    manquant = len(texte) - len(tete) - len(queue)
    return f"{tete}\n\n…[{manquant} caractères coupés]…\n\n{queue}"


def borner_texte_de_sortie(texte: str, budget: int, overflow_path: Path | None) -> str:
    """Même plafond, pour un résultat qui est une simple chaîne.

    Sert à la réponse d'un agent enfant : elle entre entière dans le contexte du
    parent, ce qui fait payer la délégation en tokens au moment précis où elle
    devrait en économiser.
    """
    if len(texte) <= budget:
        return texte
    complet = None
    if overflow_path is not None:
        with contextlib.suppress(OSError, TypeError, ValueError):
            overflow_path.parent.mkdir(parents=True, exist_ok=True)
            overflow_path.write_text(texte, encoding="utf-8")
            complet = str(overflow_path)
    reference = (
        f"\n\n[Réponse abrégée. Intégralité dans {complet} — la lire avec `read` "
        "ou la filtrer avec `search_text` plutôt que la charger en entier.]"
        if complet
        else "\n\n[Réponse abrégée; l'intégralité n'a pas pu être conservée.]"
    )
    return _tronquer(texte, budget) + reference


def _borner_recursivement(noeud: Any, budget: int) -> tuple[Any, bool]:
    if isinstance(noeud, str):
        return (_tronquer(noeud, budget), True) if len(noeud) > budget else (noeud, False)
    if isinstance(noeud, dict):
        borne: dict[Any, Any] = {}
        coupe = False
        for cle, item in noeud.items():
            borne[cle], item_coupe = _borner_recursivement(item, budget)
            coupe = coupe or item_coupe
        return borne, coupe
    if isinstance(noeud, list):
        elements = [_borner_recursivement(item, budget) for item in noeud]
        return [item for item, _ in elements], any(coupe for _, coupe in elements)
    return noeud, False


def borner_sortie_outil(
    value: Any,
    budget: int,
    overflow_path: Path | None,
) -> Any:
    """Borne ce qu'un résultat d'outil verse dans le contexte du modèle.

    Le résultat complet est écrit dans les artefacts de la session — un dossier
    que le Guardian autorise déjà en lecture — et le modèle reçoit de quoi
    décider s'il a besoin d'aller y lire : le début, la fin, la taille réelle et
    le chemin.

    Écrire ce fichier ne doit pas faire échouer l'appel : si le disque refuse,
    le résultat est simplement tronqué sans référence.
    """
    if not isinstance(value, dict):
        return value
    borne, coupe = _borner_recursivement(value.get("data"), budget)
    if not coupe:
        return value
    complet = None
    if overflow_path is not None:
        with contextlib.suppress(OSError, TypeError, ValueError):
            overflow_path.parent.mkdir(parents=True, exist_ok=True)
            overflow_path.write_text(
                json.dumps(value.get("data"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            complet = str(overflow_path)
    metadata = dict(value.get("metadata") or {})
    metadata["truncated"] = {
        "budget_characters": budget,
        "full_result_path": complet,
        "hint": (
            "Résultat abrégé pour tenir dans le contexte. "
            + (
                f"L'intégralité est dans {complet} — la lire avec `read`, "
                "ou la filtrer avec `search_text` plutôt que la charger en entier."
                if complet
                else "L'intégralité n'a pas pu être conservée."
            )
        ),
    }
    return {**value, "data": borne, "metadata": metadata}


@dataclass
class GuardianToolset(WrapperToolset[Any]):
    agent_id: str
    risks: dict[str, list[ToolRisk]]
    timeouts: dict[str, float | None]
    path_parameters: dict[str, list[str]]
    url_parameters: dict[str, list[str]]
    # Les enfants de cet agent. Non vide, ils font de lui un orchestrateur dont
    # les écritures se limitent à son espace personnel et aux données du kernel.
    delegates: tuple[str, ...] = ()

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tools = await super().get_tools(ctx)
        result: dict[str, ToolsetTool[Any]] = {}
        for name, item in tools.items():
            schema = guardian_parameters_schema(item.tool_def.parameters_json_schema)
            result[name] = replace(
                item,
                tool_def=replace(item.tool_def, parameters_json_schema=schema),
            )
        return result

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        deps = ctx.deps
        call_id = str(ctx.tool_call_id or "unknown")
        run_id = event_run_id(deps.root_run_id)
        risks = self.risks.get(name, [])
        cached_read = self._cached_read(name, tool_args, deps, run_id)
        if cached_read is not None:
            deps.events.append(
                Event(
                    session_id=deps.session_id,
                    run_id=run_id,
                    agent_id=self.agent_id,
                    type="tool.cache_hit",
                    payload={
                        "tool_call_id": call_id,
                        "tool": name,
                        "path": cached_read["data"]["path"],
                    },
                )
            )
            return cached_read
        proposed_arguments = redact(tool_args)
        if callable(getattr(deps, "secret_redactor", None)):
            proposed_arguments = deps.secret_redactor(proposed_arguments)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="tool.proposed",
                payload={"tool_call_id": call_id, "tool": name, "arguments": proposed_arguments},
            )
        )
        decision = review_tool_call(
            tool_name=name,
            tool_call_id=call_id,
            agent_id=self.agent_id,
            arguments=tool_args,
            risks=risks,
            mode=deps.security_mode,
            workspace=deps.workspace,
            trusted_read_roots=(
                (deps.state_db.parent / "sessions" / "artifacts" / str(deps.session_id)),
            )
            if deps.state_db is not None
            else (),
            workflow_grants=getattr(deps, "workflow_grants", frozenset()),
            personal_workspace=(
                deps.state_db.parent / "workspaces" / self.agent_id
                if deps.state_db is not None
                else None
            ),
            path_parameters=tuple(self.path_parameters.get(name, ())),
            url_parameters=tuple(self.url_parameters.get(name, ())),
            delegates=self.delegates,
            # `state_db` vit à la racine des données du kernel : son parent
            # couvre d'un coup l'espace personnel de l'agent, sa bibliothèque,
            # ses routines et les artefacts de session. Tout ce qu'un
            # orchestrateur tient lui-même est là-dedans.
            delegated_write_exemptions=(
                (deps.state_db.parent,) if deps.state_db is not None else ()
            ),
        )
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="guardian.reviewed",
                payload=decision.model_dump(mode="json"),
            )
        )
        if decision.verdict is GuardianVerdict.DENY:
            return ToolResult(
                ok=False,
                error={"type": "denied", "message": decision.reason},
                metadata={"guardian": decision.model_dump(mode="json")},
            ).model_dump(mode="json")
        if (
            decision.verdict is GuardianVerdict.ASK
            and not ctx.tool_call_approved
            and not deps.is_scope_approved(decision)
        ):
            request = ApprovalRequest(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                tool_call_id=call_id,
                tool_name=name,
                tool_description=tool.tool_def.description or "",
                action_family=action_family(risks),
                path=decision.path,
                justification=decision.justification,
                arguments=proposed_arguments,
                risks=risks,
                reason=decision.reason,
            )
            deps.pending_approvals[call_id] = request
            deps.events.append(
                Event(
                    session_id=deps.session_id,
                    run_id=run_id,
                    agent_id=self.agent_id,
                    type="approval.requested",
                    payload=request.model_dump(mode="json"),
                )
            )
            raise ApprovalRequired(metadata={"approval_id": str(request.approval_id)})
        clean_args = dict(tool_args)
        clean_args.pop("justification", None)
        clean_args.pop("refresh", None)
        started = time.monotonic()
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="tool.started",
                payload={"tool_call_id": call_id, "tool": name, "path": decision.path},
            )
        )
        try:
            timeout = self.timeouts.get(name)
            if timeout is None:
                value = await self.wrapped.call_tool(name, clean_args, ctx, tool)
            else:
                async with asyncio.timeout(timeout):
                    value = await self.wrapped.call_tool(name, clean_args, ctx, tool)
            value = ToolResult.model_validate(value).model_dump(mode="json")
        except Exception as exc:
            error_type = (
                "validation"
                if isinstance(exc, (ValueError, TypeError))
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "not_found"
                if isinstance(exc, FileNotFoundError)
                else "execution"
            )
            error_message: Any = str(exc)[:4000]
            if callable(getattr(deps, "secret_redactor", None)):
                error_message = deps.secret_redactor(error_message)
            failure = {
                "ok": False,
                "data": None,
                "error": {"type": error_type, "message": str(error_message)},
                "metadata": {},
            }
            deps.events.append(
                Event(
                    session_id=deps.session_id,
                    run_id=run_id,
                    agent_id=self.agent_id,
                    type="tool.failed",
                    payload={
                        "tool_call_id": call_id,
                        "tool": name,
                        "duration_ms": (time.monotonic() - started) * 1000,
                        "error": failure["error"],
                    },
                )
            )
            return failure
        if callable(getattr(deps, "secret_redactor", None)):
            value = deps.secret_redactor(value)
        # Borner après la rédaction des secrets : le fichier de débordement ne
        # doit pas contenir ce que le contexte n'a pas le droit de voir.
        value = borner_sortie_outil(
            value,
            BUDGET_SORTIE_OUTIL,
            (
                deps.state_db.parent
                / "sessions"
                / "artifacts"
                / str(deps.session_id)
                / f"{name}-{call_id}.json"
            )
            if deps.state_db is not None
            else None,
        )
        self._remember_read(name, tool_args, value, deps, run_id)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="tool.completed",
                payload={
                    "tool_call_id": call_id,
                    "tool": name,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "result": redact(value),
                },
            )
        )
        return value

    def _read_cache_key(
        self,
        name: str,
        arguments: dict[str, Any],
        deps: Any,
        run_id: Any,
    ) -> tuple[tuple[str, str, int, int], Path] | None:
        if name != "read" or bool(arguments.get("refresh")):
            return None
        target = canonical_path(arguments.get("path"), deps.workspace)
        if target is None:
            return None
        return (
            (
                str(run_id),
                str(target),
                int(arguments.get("offset", 1)),
                int(arguments.get("limit", 2000)),
            ),
            target,
        )

    def _cached_read(
        self,
        name: str,
        arguments: dict[str, Any],
        deps: Any,
        run_id: Any,
    ) -> dict[str, Any] | None:
        resolved = self._read_cache_key(name, arguments, deps, run_id)
        if resolved is None:
            return None
        key, target = resolved
        cached = deps.read_cache.get(key)
        if cached is None:
            return None
        try:
            stat = target.stat()
        except OSError:
            return None
        if (stat.st_mtime_ns, stat.st_size) != cached["signature"]:
            deps.read_cache.pop(key, None)
            return None
        return ToolResult(
            ok=True,
            data={
                "ok": True,
                "path": str(target),
                "unchanged": True,
                "sha256": cached.get("sha256"),
                "content": "",
                "hint": (
                    "Lecture identique déjà fournie dans ce run. Utilise le contenu "
                    "précédent; appelle read avec refresh=true seulement si les octets "
                    "sont réellement nécessaires de nouveau."
                ),
            },
            metadata={"cache_hit": True},
        ).model_dump(mode="json")

    def _remember_read(
        self,
        name: str,
        arguments: dict[str, Any],
        value: dict[str, Any],
        deps: Any,
        run_id: Any,
    ) -> None:
        resolved = self._read_cache_key(name, arguments, deps, run_id)
        if resolved is None or not value.get("ok"):
            return
        key, target = resolved
        data = value.get("data")
        try:
            stat = target.stat()
        except OSError:
            return
        deps.read_cache[key] = {
            "signature": (stat.st_mtime_ns, stat.st_size),
            "sha256": data.get("sha256") if isinstance(data, dict) else None,
        }
