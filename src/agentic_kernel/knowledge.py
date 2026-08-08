"""Bibliothèque de connaissance transverse, en markdown lisible.

Distincte de la connaissance projet — `DECISION.md`, `MEMORY.md` — qui vit dans
le dépôt et voyage avec lui. Celle-ci appartient au poste : documents déposés,
pages archivées, références de travail.

    knowledge/
      incoming/    déposés à la main, tous formats, vidés à l'ingestion
      library/     une page par sujet — état courant, seul corpus indexé
      journal/     une page par mois — histoire des ingestions, jamais indexée

Une encyclopédie, pas un journal de bord : réingérer un sujet réécrit sa page
au lieu d'en créer une seconde. Sans cela, une veille produisait « sujet »,
« sujet-2 », « sujet-3 », et la recherche renvoyait trois états successifs sans
dire lequel faisait foi.

La réécriture est confiée au modèle et peut donc perdre de l'information. Le
journal conserve chaque version remplacée : il est le filet, la bibliothèque
n'étant pas versionnée. Il reste hors de l'index — l'y inclure ferait remonter
dans les résultats les traces datées que la page par sujet vient d'éliminer.

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


def _front_matter(path: Path) -> dict[str, str]:
    """Champs du frontmatter, lus sans dépendre de sa validité stricte."""
    try:
        contenu = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not contenu.startswith("---"):
        return {}
    champs: dict[str, str] = {}
    for ligne in contenu.split("---", 2)[1].splitlines():
        cle, separateur, valeur = ligne.partition(":")
        if not separateur:
            continue
        champs[cle.strip()] = valeur.strip().strip('"').strip("[]")
    return champs


class KnowledgeLibrary:
    """Écriture et inventaire de la bibliothèque."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.incoming = root / "incoming"
        self.library = root / "library"
        self.journal = root / "journal"

    def ensure(self) -> None:
        self.incoming.mkdir(parents=True, exist_ok=True)
        self.library.mkdir(parents=True, exist_ok=True)
        self.journal.mkdir(parents=True, exist_ok=True)
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
        """Page du sujet. Le même titre désigne toujours la même page.

        Suffixer en cas de collision produisait `sujet.md`, `sujet-2.md`,
        `sujet-3.md` : un journal déguisé, où la recherche renvoyait trois
        états successifs du même sujet sans dire lequel faisait foi.
        """
        return self.library / f"{slugify(title)}.md"

    def write(self, document: Document, *, ingested: str | None = None) -> Path:
        """Écrit la page du sujet, en archivant la version qu'elle remplace.

        La réécriture est confiée au modèle, qui peut donc perdre de
        l'information. Le journal conserve l'état précédent : c'est le seul
        filet, la bibliothèque n'étant pas versionnée.
        """
        self.ensure()
        target = self.target_path(document.title)
        previous = (
            target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
        )
        content = f"{render_frontmatter(document, ingested=ingested)}\n\n{document.body.strip()}\n"
        target.write_text(content, encoding="utf-8")
        self.record(
            title=document.title,
            source=document.source,
            page=target,
            replaced=previous,
            at=ingested,
        )
        return target

    def journal_path(self, at: datetime | None = None) -> Path:
        """Une page par mois : un fichier par ingestion en produirait des
        centaines, illisibles et pénibles à parcourir."""
        moment = at or datetime.now(UTC)
        return self.journal / f"{moment:%Y-%m}.md"

    def record(
        self,
        *,
        title: str,
        source: str,
        page: Path,
        replaced: str | None = None,
        at: str | None = None,
    ) -> Path:
        """Ajoute une entrée au journal, sans jamais rien y effacer.

        Le journal n'est pas indexé : l'y inclure ferait remonter dans les
        recherches les traces datées et les versions périmées, exactement le
        bruit que la page par sujet supprime.
        """
        self.ensure()
        moment = datetime.now(UTC)
        target = self.journal_path(moment)
        entete = "" if target.exists() else f"# Journal {moment:%Y-%m}\n\n"
        lignes = [
            f"## {at or moment.isoformat(timespec='seconds')} — {title}",
            "",
            f"- Source : {source or 'inconnue'}",
            f"- Page : `{page.name}`",
        ]
        if replaced is not None:
            lignes += [
                "",
                "<details><summary>Version remplacée</summary>",
                "",
                "````markdown",
                replaced.rstrip(),
                "````",
                "",
                "</details>",
            ]
        with target.open("a", encoding="utf-8") as flux:
            flux.write(entete + "\n".join(lignes) + "\n\n")
        return target

    def inventory(self) -> list[dict[str, object]]:
        """Index des pages : titre, tags, date, taille.

        Sert l'écran de sélection et le mode automatique. Le frontmatter est lu
        ligne à ligne plutôt que par un analyseur YAML : une page éditée à la
        main dans Obsidian peut avoir un en-tête légèrement irrégulier, et il
        vaut mieux la lister avec un titre approximatif que de la faire
        disparaître de l'index.
        """
        if not self.library.is_dir():
            return []
        pages: list[dict[str, object]] = []
        for path in sorted(self.library.glob("*.md")):
            if path.name == TAGS_FILE:
                continue
            entete = _front_matter(path)
            pages.append(
                {
                    "slug": path.stem,
                    "title": entete.get("title") or path.stem,
                    "tags": [tag for tag in entete.get("tags", "").split(",") if tag.strip()],
                    "ingested": entete.get("ingested", ""),
                    "source": entete.get("source", ""),
                    "summary": entete.get("summary", ""),
                    "bytes": path.stat().st_size,
                }
            )
        return pages

    def page(self, slug: str) -> str | None:
        """Contenu d'une page, ou None si le nom ne désigne rien de légitime.

        Le nom vient de la surface : il est réduit à son slug pour qu'aucun
        chemin ne puisse sortir de la bibliothèque.
        """
        candidate = self.library / f"{slugify(slug)}.md"
        if not candidate.is_file() or candidate.name == TAGS_FILE:
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")

    def pending(self) -> list[Path]:
        """Documents déposés à la main, en attente de conversion."""
        if not self.incoming.is_dir():
            return []
        return sorted(
            path
            for path in self.incoming.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )
