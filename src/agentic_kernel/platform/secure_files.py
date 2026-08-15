from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

# Sous Windows, `open()` échoue avec FileNotFoundError quand le chemin complet
# dépasse MAX_PATH (260 caractères) — y compris quand le répertoire vient d'être
# créé. Le préfixe `\\?\` fait contourner la limite à l'API Win32 large.
_LONG_PATH_THRESHOLD = 240


def long_path(path: Path) -> Path:
    r"""Préfixe `\\?\` quand le chemin dépasse la limite Windows.

    Les chemins profonds — espace personnel sous un dossier long, tests avec
    répertoires temporaires imbriqués, artefacts de session composés de
    plusieurs uuid — produisent des erreurs « No such file or directory »
    déroutantes sans ce préfixe.
    """
    if os.name != "nt" or not path.is_absolute():
        return path
    text = str(path)
    if len(text) < _LONG_PATH_THRESHOLD or text.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\" + text)


def secure_file(path: Path) -> None:
    """Restrict a sensitive file to its owner using native platform controls."""
    path = path.resolve()
    if os.name != "nt":
        path.chmod(0o600)
        return
    icacls = shutil.which("icacls")
    if icacls is None:
        raise PermissionError("icacls is required to protect sensitive files on Windows")
    principal = getpass.getuser()
    result = subprocess.run(  # noqa: S603 - icacls absolu (shutil.which), drapeaux fixes
        [
            icacls,
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(F)",
            "/grant:r",
            "SYSTEM:(F)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PermissionError(f"failed to secure {path}: {result.stderr.strip()}")
