"""Matérialisation du socle d'installation dans l'espace utilisateur.

`content-agents/` n'est pas versionné : il contient des données et des secrets
propres à chaque poste. Mais les skills de base, le prompt système et l'agent
`main` sont des ressources de l'application. Sans ce socle, installer AMK
ailleurs supposait de copier `content-agents/` — ce qui transportait aussi
l'état local : routines pointant vers des dossiers inexistants, mémoires d'une
autre machine, clés API.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS_ROOT = Path(__file__).resolve().parent / "defaults"

# Gabarits : copiés sous leur nom d'exemple, jamais sous le nom réel. Renseigner
# la configuration reste une décision explicite de l'utilisateur, et un fichier
# `providers.json` créé d'office masquerait le fait qu'aucune clé n'est posée.
#
# Seul `providers.json` est indispensable au démarrage : `secrets.json` et
# `rag.json` ont des comportements de repli. Les signaler tous à chaque
# lancement produirait un avertissement permanent sur une installation saine.
REQUIRED_CONFIG = {"providers.json": "providers.example.json"}
OPTIONAL_CONFIG = {
    "secrets.json": "secrets.example.json",
    "rag.json": "rag.example.json",
}

# Jamais matérialisé automatiquement : sans valeur, ces fichiers n'apportent
# rien, et une liste de chemins n'a de sens que sur la machine qui l'a écrite.
SKIPPED = {"README.md"}
MANIFEST_FILE = ".amk-defaults.json"


@dataclass
class BootstrapReport:
    content_root: Path
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    @property
    def initialized(self) -> bool:
        return bool(self.created or self.updated)

    def render(self) -> str:
        lines = [f"Espace de travail : {self.content_root}"]
        if self.created:
            lines.append(f"  {len(self.created)} élément(s) installé(s) :")
            lines.extend(f"    + {name}" for name in sorted(self.created))
        if self.updated:
            lines.append(f"  {len(self.updated)} élément(s) mis à jour :")
            lines.extend(f"    ~ {name}" for name in sorted(self.updated))
        if self.kept:
            lines.append(f"  {len(self.kept)} élément(s) déjà présent(s), inchangé(s)")
        if not self.created and not self.kept:
            lines.append("  socle introuvable : installation incomplète")
        return "\n".join(lines)


def ensure_content_root(content_root: Path, *, defaults: Path | None = None) -> BootstrapReport:
    """Installe ce qui manque, sans jamais écraser ce qui existe.

    L'idempotence est le point important : la fonction tourne à chaque
    démarrage. Elle restaure une skill supprimée par accident, mais respecte
    toute skill que l'utilisateur a modifiée.
    """
    source = defaults or DEFAULTS_ROOT
    report = BootstrapReport(content_root=content_root)
    if not source.is_dir():
        return report

    content_root.mkdir(parents=True, exist_ok=True)
    # `workspace: null` resolves to this personal, user-visible directory for
    # the bundled main agent. Other agents are materialized lazily.
    (content_root / "workspaces" / "main").mkdir(parents=True, exist_ok=True)
    manifest_path = content_root / MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        previous = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    except (OSError, json.JSONDecodeError):
        previous = {}
    tracked: dict[str, dict[str, object]] = {}
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if relative.name in SKIPPED and relative.parent == Path("."):
            continue
        target = content_root / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        name = relative.as_posix()
        source_hash = _digest(item)
        if target.exists():
            target_hash = _digest(target)
            record = previous.get(name, {}) if isinstance(previous, dict) else {}
            was_managed = bool(record.get("managed")) if isinstance(record, dict) else False
            previous_hash = record.get("source_hash") if isinstance(record, dict) else None
            if was_managed and target_hash == previous_hash and source_hash != previous_hash:
                _copy_atomic(item, target)
                report.updated.append(name)
                tracked[name] = {"source_hash": source_hash, "managed": True}
            else:
                # Exact copies can safely rejoin the managed update channel;
                # anything else is an explicit user variant and remains fixed.
                managed = target_hash == source_hash
                tracked[name] = {"source_hash": source_hash, "managed": managed}
                report.kept.append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_atomic(item, target)
        tracked[name] = {"source_hash": source_hash, "managed": True}
        report.created.append(name)
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"schema_version": 1, "files": tracked}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return report


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_atomic(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.amk.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def missing_configuration(content_root: Path, *, include_optional: bool = False) -> list[str]:
    """Signale la configuration qui manque encore pour démarrer.

    Séparé de l'installation : un socle complet ne suffit pas à faire tourner
    l'application, il faut encore une configuration de provider.
    """
    candidates = dict(REQUIRED_CONFIG)
    if include_optional:
        candidates.update(OPTIONAL_CONFIG)
    return [
        real
        for real, template in sorted(candidates.items())
        if not (content_root / real).exists() and (content_root / template).exists()
    ]
