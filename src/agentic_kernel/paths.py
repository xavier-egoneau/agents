"""Resolve application assets, user data and workspaces independently.

Historically AMK treated the current working directory as all three.  That is
useful in a source checkout, but makes ``amk web`` create a partial installation
wherever the command happens to be launched.  The application root is now
discovered from an explicit override or from the installed/source package; the
current directory remains only the default workspace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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


def content_root(application: Path) -> Path:
    """Return the stable user-content directory.

    Existing source checkouts keep their legacy ``content-agents`` directory so
    credentials and sessions are not silently abandoned. Fresh installations
    use the platform data directory and are therefore independent of the CWD.
    """
    override = os.environ.get("AMK_HOME")
    if override:
        return Path(override).expanduser().resolve() / "content-agents"
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
