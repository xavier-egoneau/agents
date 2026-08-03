from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .platform.secure_files import secure_file

SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class SecretStore:
    """Local JSON secret storage whose values are never returned by its API."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def set(self, name: str, value: str) -> None:
        normalized = name.strip()
        if not SECRET_NAME.fullmatch(normalized):
            raise ValueError(
                "le nom doit suivre le format d’une variable "
                "(lettres, chiffres, `.`, `-` et `_`, sans commencer par un chiffre)"
            )
        if not value:
            raise ValueError("la valeur du secret est absente")
        with self._lock:
            document = self._read()
            document[normalized] = value
            self._write(document)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    def resolve(self, name: str) -> str | None:
        """Resolve a value for trusted kernel/tool code; never expose this to a model."""
        with self._lock:
            return self._read().get(name)

    def redact(self, value: Any) -> Any:
        """Remove every known secret value, regardless of the surrounding key."""
        with self._lock:
            secrets = sorted(
                (secret for secret in self._read().values() if secret),
                key=len,
                reverse=True,
            )

        def clean(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: clean(child) for key, child in item.items()}
            if isinstance(item, list):
                return [clean(child) for child in item]
            if isinstance(item, tuple):
                return tuple(clean(child) for child in item)
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "***")
                return item
            return item

        return clean(value)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"fichier de secrets invalide : {self.path}") from exc
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ConfigurationError(f"fichier de secrets invalide : {self.path}")
        return raw

    def _write(self, document: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            secure_file(temporary)
            os.replace(temporary, self.path)
            secure_file(self.path)
        finally:
            temporary.unlink(missing_ok=True)
