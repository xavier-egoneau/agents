"""Schema-driven module settings with write-only secret fields."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .models import ModuleConfigField, ModuleManifest
from .modules import ModuleRegistry
from .platform.secure_files import secure_file
from .secrets import SecretStore


class ModuleSettingsStore:
    def __init__(self, content_root: Path, registry: ModuleRegistry) -> None:
        self.content_root = content_root
        self.path = content_root / "tool-settings.json"
        self.registry = registry
        self.secrets = SecretStore(content_root / "secrets.json")
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        document = self._read()
        secret_names = set(self.secrets.names())
        return [
            self._view(manifest, document, secret_names)
            for manifest in self._configurable_manifests()
        ]

    def update(  # noqa: C901 - dette: mise à jour multi-cas
        self, module_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        manifests = {item.id: item for item in self._configurable_manifests()}
        manifest = manifests.get(module_id)
        if manifest is None or manifest.config is None:
            raise ConfigurationError(f"module configurable introuvable : {module_id}")
        fields = {field.name: field for field in manifest.config.fields}
        unknown = set(values) - fields.keys()
        if unknown:
            raise ConfigurationError(f"paramètres inconnus : {', '.join(sorted(unknown))}")

        normalized_values: dict[str, Any] = {}
        pending_secrets: dict[str, str] = {}
        existing_secrets = set(self.secrets.names())
        for name, raw in values.items():
            field = fields[name]
            if field.type == "secret":
                if isinstance(raw, str) and raw:
                    pending_secrets[field.secret_name or name] = raw
                elif raw not in (None, ""):
                    raise ConfigurationError(f"{field.label} doit être une chaîne")
                continue
            normalized_values[name] = _normalize(field, raw)
        missing_secrets = [
            field.label
            for field in fields.values()
            if field.type == "secret"
            and field.required
            and field.secret_name not in existing_secrets
            and field.secret_name not in pending_secrets
        ]
        if missing_secrets:
            raise ConfigurationError(f"secrets requis : {', '.join(missing_secrets)}")

        with self._lock:
            document = self._read()
            modules = document.setdefault("modules", {})
            current = dict(modules.get(module_id, {}))
            for name, normalized in normalized_values.items():
                if normalized is None:
                    current.pop(name, None)
                else:
                    current[name] = normalized
            for secret_name, secret_value in pending_secrets.items():
                self.secrets.set(secret_name, secret_value)
            modules[module_id] = current
            self._write(document)
        return self._view(manifest, document, set(self.secrets.names()))

    def effective(self, module_id: str) -> dict[str, Any]:
        """Return non-secret effective values for trusted runtime services."""
        manifest = next(
            (item for item in self._configurable_manifests() if item.id == module_id),
            None,
        )
        if manifest is None or manifest.config is None:
            return {}
        return self._effective_values(manifest, self._read())

    def _configurable_manifests(self) -> list[ModuleManifest]:
        return [
            item
            for item in self.registry.discover().modules
            if item.enabled and item.config is not None
        ]

    def _effective_values(
        self, manifest: ModuleManifest, document: dict[str, Any]
    ) -> dict[str, Any]:
        assert manifest.config is not None  # noqa: S101 - invariant interne, appelants vérifiés
        values = {
            field.name: field.default
            for field in manifest.config.fields
            if field.type != "secret" and field.default is not None
        }
        if manifest.config.legacy_file:
            legacy_path = self.content_root / manifest.config.legacy_file
            if legacy_path.is_file():
                try:
                    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ConfigurationError(
                        f"configuration historique invalide : {legacy_path}"
                    ) from exc
                if isinstance(legacy, dict):
                    allowed = {field.name for field in manifest.config.fields}
                    values.update({key: value for key, value in legacy.items() if key in allowed})
        modules = document.get("modules", {})
        stored = modules.get(manifest.id, {}) if isinstance(modules, dict) else {}
        if isinstance(stored, dict):
            values.update(stored)
        return values

    def _view(
        self,
        manifest: ModuleManifest,
        document: dict[str, Any],
        secret_names: set[str],
    ) -> dict[str, Any]:
        assert manifest.config is not None  # noqa: S101 - invariant interne, appelants vérifiés
        effective = self._effective_values(manifest, document)
        fields: list[dict[str, Any]] = []
        complete = True
        customized = bool(
            manifest.config.legacy_file
            and (self.content_root / manifest.config.legacy_file).is_file()
        )
        stored = document.get("modules", {}).get(manifest.id, {})
        for field in manifest.config.fields:
            item = field.model_dump(mode="json")
            if field.type == "secret":
                configured = bool(field.secret_name and field.secret_name in secret_names)
                item["configured"] = configured
                complete = complete and (configured or not field.required)
                customized = customized or configured
            else:
                value = effective.get(field.name)
                item["value"] = value
                complete = complete and (value not in (None, "") or not field.required)
                customized = customized or (
                    isinstance(stored, dict) and field.name in stored
                )
            fields.append(item)
        state = "configured" if complete and customized else "defaults" if complete else "required"
        return {
            "id": manifest.id,
            "name": manifest.name,
            "description": manifest.config.description or manifest.description,
            "title": manifest.config.title,
            "applies_to": manifest.config.applies_to,
            "state": state,
            "fields": fields,
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "modules": {}}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"fichier de paramètres invalide : {self.path}") from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("modules"), dict)
        ):
            raise ConfigurationError(f"fichier de paramètres invalide : {self.path}")
        return document

    def _write(self, document: dict[str, Any]) -> None:
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


def _normalize(  # noqa: C901 - dette: normalisation par type de champ
    field: ModuleConfigField, value: Any
) -> Any:
    if value in (None, ""):
        if field.required:
            raise ConfigurationError(f"{field.label} est requis")
        return None
    if field.type in {"text", "file", "directory", "select"}:
        if not isinstance(value, str):
            raise ConfigurationError(f"{field.label} doit être une chaîne")
        if field.type == "select" and value not in field.options:
            raise ConfigurationError(f"valeur invalide pour {field.label}")
        return value.strip()
    if field.type == "boolean":
        if not isinstance(value, bool):
            raise ConfigurationError(f"{field.label} doit être un booléen")
        return value
    if field.type == "string_list":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigurationError(f"{field.label} doit être une liste de chaînes")
        return [item for item in (entry.strip() for entry in value) if item]
    if field.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{field.label} doit être un entier")
    elif field.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigurationError(f"{field.label} doit être un nombre")
    else:
        raise ConfigurationError(f"type de paramètre non pris en charge : {field.type}")
    numeric = float(value)
    if field.minimum is not None and numeric < field.minimum:
        raise ConfigurationError(f"{field.label} doit être supérieur ou égal à {field.minimum}")
    if field.maximum is not None and numeric > field.maximum:
        raise ConfigurationError(f"{field.label} doit être inférieur ou égal à {field.maximum}")
    return value


def stored_module_settings(content_root: Path, module_id: str) -> dict[str, Any]:
    """Read one validated non-secret settings namespace at runtime."""
    path = content_root / "tool-settings.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"fichier de paramètres invalide : {path}") from exc
    modules = document.get("modules") if isinstance(document, dict) else None
    values = modules.get(module_id) if isinstance(modules, dict) else None
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ConfigurationError(f"paramètres invalides pour le module {module_id}")
    return dict(values)
