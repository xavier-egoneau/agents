"""Stable identity and platform data directory for one local AMK install."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from uuid import uuid4

INSTALLATION_FILE = "installation.json"


def data_home() -> Path:
    override = os.environ.get("AMK_DATA_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "amk"


def installation_id(root: Path | None = None) -> str:
    """Return one opaque identifier, created atomically on first use."""
    directory = (root or data_home()).expanduser()
    marker = directory / INSTALLATION_FILE
    identifier = uuid4().hex
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return identifier
    for _ in range(100):
        try:
            existing = marker.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        try:
            # Exclusive creation makes the first writer authoritative. A
            # second starter may observe the empty file between open and write,
            # hence the bounded retry loop around both operations.
            with marker.open("x", encoding="utf-8") as stream:
                stream.write(identifier)
                stream.flush()
                os.fsync(stream.fileno())
            return identifier
        except FileExistsError:
            time.sleep(0.01)
        except OSError:
            # Read-only data homes remain usable for this process, but doctor
            # will report that identity persistence is unavailable.
            return identifier
    try:
        return marker.read_text(encoding="utf-8").strip() or identifier
    except OSError:
        return identifier
