from __future__ import annotations

import platform
import subprocess


class NativeDialogUnavailable(RuntimeError):
    pass


def choose_directory(prompt: str) -> str:
    """Open the native folder chooser and return an empty string on cancellation."""
    if platform.system() == "Darwin":
        result = subprocess.run(  # noqa: S603 - osascript fixe, prompt interne
            ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],  # noqa: S607
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if "User canceled" in result.stderr or "-128" in result.stderr:
            return ""
        raise NativeDialogUnavailable(result.stderr.strip() or "folder chooser unavailable")

    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise NativeDialogUnavailable("Tk is unavailable on this Python installation") from exc
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return str(filedialog.askdirectory(parent=root, title=prompt, mustexist=True))
        finally:
            root.destroy()
    except tk.TclError as exc:
        raise NativeDialogUnavailable(f"native folder chooser unavailable: {exc}") from exc
