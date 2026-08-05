from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path


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
    result = subprocess.run(
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
