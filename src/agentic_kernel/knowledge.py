"""Bibliothèque de connaissance transverse, en markdown lisible.

Distincte de la connaissance projet — `DECISION.md`, `MEMORY.md` — qui vit dans
le dépôt et voyage avec lui. Celle-ci appartient au poste : documents déposés,
pages archivées, références de travail.

    content-agents/knowledge/
      incoming/    déposés à la main, tous formats, vidés à l'ingestion
      library/     markdown convertis — source de vérité, ouvrable dans Obsidian

Dossier plat et frontmatter YAML plutôt qu'une arborescence thématique : un
document relève souvent de plusieurs sujets, une hiérarchie force un choix
unique et le corriger casse les liens. L'index fait le travail de recherche;
l'arborescence ne serait qu'un confort de lecture, payé cher.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TAGS_FILE = "_tags.md"

# Vocabulaire livré à la première création. Sans liste tenue, chaque ingestion
# invente ses propres mots et l'on se retrouve avec dix étiquettes pour trois
# idées — une taxonomie qui ne filtre plus rien.
DEFAULT_TAGS = """# Vocabulaire des tags

Liste des tags autorisés dans le frontmatter de cette bibliothèque. En ajouter
un ici avant de l'utiliser, plutôt que d'en inventer à chaque ingestion : une
taxonomie qui dérive ne filtre plus rien.

- architecture
- agents
- reference
- outil
- veille
- specification
"""


@dataclass(frozen=True)
class Document:
    """Un document converti, prêt à être écrit dans la bibliothèque."""

    title: str
    body: str
    source: str
    source_type: str
    tags: tuple[str, ...] = ()
    summary: str = ""


def slugify(value: str, *, max_length: int = 60) -> str:
    """Nom de fichier stable, lisible et valide sur les trois systèmes.

    Obsidian affiche le nom de fichier : il doit rester lisible. Windows
    interdit `<>:"/\\|?*`, d'où un jeu de caractères volontairement étroit.
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)[:max_length].strip("-")
    return slug or "document"


def _yaml_value(value: str) -> str:
    """Échappe une valeur scalaire YAML.

    Un titre contenant `:` ou commençant par `-` casse silencieusement le
    frontmatter, et Obsidian cesse alors d'afficher les propriétés.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    return f'"{escaped}"'


def render_frontmatter(document: Document, *, ingested: str | None = None) -> str:
    lines = [
        "---",
        f"title: {_yaml_value(document.title)}",
        f"source: {_yaml_value(document.source)}",
        f"source_type: {document.source_type}",
        f"ingested: {ingested or datetime.now(UTC).date().isoformat()}",
    ]
    if document.summary:
        lines.append(f"summary: {_yaml_value(document.summary)}")
    tags = ", ".join(sorted(set(document.tags)))
    lines.append(f"tags: [{tags}]")
    lines.append("---")
    return "\n".join(lines)


class KnowledgeLibrary:
    """Écriture et inventaire de la bibliothèque."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.incoming = root / "incoming"
        self.library = root / "library"

    def ensure(self) -> None:
        self.incoming.mkdir(parents=True, exist_ok=True)
        self.library.mkdir(parents=True, exist_ok=True)
        tags = self.library / TAGS_FILE
        if not tags.exists():
            tags.write_text(DEFAULT_TAGS, encoding="utf-8")

    def known_tags(self) -> set[str]:
        try:
            content = (self.library / TAGS_FILE).read_text(encoding="utf-8")
        except OSError:
            return set()
        return {
            line.lstrip("- ").strip()
            for line in content.splitlines()
            if line.startswith("- ") and line.lstrip("- ").strip()
        }

    def target_path(self, title: str) -> Path:
        """Chemin de destination, suffixé seulement en cas de collision réelle."""
        base = slugify(title)
        candidate = self.library / f"{base}.md"
        index = 2
        while candidate.exists():
            candidate = self.library / f"{base}-{index}.md"
            index += 1
        return candidate

    def write(self, document: Document, *, ingested: str | None = None) -> Path:
        self.ensure()
        target = self.target_path(document.title)
        content = f"{render_frontmatter(document, ingested=ingested)}\n\n{document.body.strip()}\n"
        target.write_text(content, encoding="utf-8")
        return target

    def pending(self) -> list[Path]:
        """Documents déposés à la main, en attente de conversion."""
        if not self.incoming.is_dir():
            return []
        return sorted(
            path
            for path in self.incoming.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )
