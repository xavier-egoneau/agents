"""Resolve application assets, user data and workspaces independently.

Historically AMK treated the current working directory as all three.  That is
useful in a source checkout, but makes ``amk web`` create a partial installation
wherever the command happens to be launched.  The application root is now
discovered from an explicit override or from the installed/source package; the
current directory remains only the default workspace.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .installation import data_home


def _is_application_root(candidate: Path) -> bool:
    return (candidate / "tools" / "modules").is_dir() and (
        candidate / "surfaces" / "web" / "package.json"
    ).is_file()


def _ancestors(start: Path):
    yield start
    yield from start.parents


def application_root(start: Path | None = None) -> Path:
    """Locate immutable AMK application assets without depending on the CWD."""
    override = os.environ.get("AMK_APP_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
        if not _is_application_root(candidate):
            raise ConfigurationError(f"invalid AMK_APP_ROOT: {candidate}")
        return candidate

    candidates: list[Path] = []
    if start is not None:
        candidates.extend(_ancestors(start.expanduser().resolve()))
    # Keep source checkouts convenient even when the command is launched from
    # another directory. Packaged distributions can place their bundled assets
    # above this module and use the same discovery contract.
    candidates.extend(_ancestors(Path(__file__).resolve().parent))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_application_root(candidate):
            return candidate
    raise ConfigurationError(
        "AMK application assets are unavailable; reinstall AMK or set AMK_APP_ROOT"
    )


SETTINGS_FILE = "settings.json"


def settings_path() -> Path:
    """Préférences de l'installation, hors de `content-agents`.

    Elles ne peuvent pas y vivre : c'est précisément l'emplacement de ce dossier
    qu'elles décrivent. Elles restent donc dans le répertoire de données de la
    plateforme, qui ne bouge pas.
    """
    return data_home().resolve() / SETTINGS_FILE


def configured_home() -> Path | None:
    """Emplacement choisi par l'utilisateur, ou None s'il n'a rien choisi."""
    path = settings_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Un fichier absent est le cas normal; un fichier illisible ne doit pas
        # empêcher AMK de démarrer sur son emplacement par défaut.
        return None
    raw = document.get("content_home")
    if not raw:
        return None
    # Normalisé à la lecture, et pas seulement à l'écriture : un réglage déjà
    # enregistré pointant sur un `content-agents` doit être réinterprété, sinon
    # le correctif ne répare que les saisies futures et laisse l'installation
    # cassée.
    return normalized_home(Path(str(raw)))


CONTENT_DIRECTORY = "content-agents"


def normalized_home(selected: Path) -> Path:
    """Ramène au dossier *parent* de `content-agents`.

    Le réglage désigne le parent, mais l'écran parle du contenu — agents,
    skills, sessions. Pointer directement sur un `content-agents` existant est
    donc le geste naturel, et produisait `…/content-agents/content-agents`.

    On accepte les deux formes : un dossier nommé `content-agents`, ou qui en a
    la forme, est traité comme la cible elle-même et c'est son parent qui est
    retenu.
    """
    selected = Path(selected).expanduser().resolve()
    if selected.name == CONTENT_DIRECTORY:
        return selected.parent
    # Un dossier renommé reste reconnaissable à ce qu'il contient.
    if (selected / "system.md").is_file() and (selected / "agents").is_dir():
        return selected.parent
    return selected


def store_home(home: Path | None) -> None:
    """Écrit l'emplacement choisi, ou l'efface pour revenir au défaut."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = {}
    if home is None:
        document.pop("content_home", None)
    else:
        document["content_home"] = str(Path(home).expanduser().resolve())
    # Écriture atomique : une coupure au mauvais moment laisserait un fichier
    # tronqué, et AMK repartirait silencieusement sur le mauvais dossier.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def content_root(application: Path) -> Path:
    """Return the stable user-content directory.

    Existing source checkouts keep their legacy ``content-agents`` directory so
    credentials and sessions are not silently abandoned. Fresh installations
    use the platform data directory and are therefore independent of the CWD.

    L'ordre est délibéré : la variable d'environnement l'emporte sur le réglage
    enregistré, pour qu'un lancement ponctuel sur un autre jeu de données
    n'écrase jamais la préférence de l'utilisateur.
    """
    override = os.environ.get("AMK_HOME")
    if override:
        return Path(override).expanduser().resolve() / "content-agents"
    configured = configured_home()
    if configured:
        return configured / "content-agents"
    legacy = application.resolve() / "content-agents"
    if legacy.exists():
        return legacy
    return data_home().resolve() / "content-agents"


@dataclass(frozen=True)
class RuntimeLayout:
    application_root: Path
    content_root: Path
    workspace: Path


def runtime_layout(workspace: Path | None = None) -> RuntimeLayout:
    application = application_root(Path.cwd())
    return RuntimeLayout(
        application_root=application,
        content_root=content_root(application),
        workspace=(workspace or Path.cwd()).expanduser().resolve(),
    )
