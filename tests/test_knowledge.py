"""Bibliothèque de connaissance : conversion, classement, frontmatter.

Corpus distinct de la connaissance projet (`DECISION.md`, `MEMORY.md`), qui vit
dans le dépôt. Celui-ci appartient au poste et se remplit par ingestion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_kernel.converters import (
    ConversionError,
    convert_file,
    html_to_markdown,
)
from agentic_kernel.knowledge import (
    TAGS_FILE,
    Document,
    KnowledgeLibrary,
    render_frontmatter,
    slugify,
)
from agentic_kernel.web_capture import _extract, is_url

PAGE = """<html><head><title>Patterns d'agents</title><style>.a{color:red}</style></head>
<body><h1>Introduction</h1><p>Un agent <b>autonome</b> choisit ses outils.</p>
<ul><li>Recherche</li><li>Synthèse</li></ul>
<script>track()</script></body></html>"""


@pytest.fixture
def library(tmp_path: Path) -> KnowledgeLibrary:
    instance = KnowledgeLibrary(tmp_path / "knowledge")
    instance.ensure()
    return instance


def test_slug_stays_readable_and_portable() -> None:
    """Obsidian affiche le nom de fichier, Windows interdit certains caractères."""
    assert slugify("Éléments d'architecture : agents/outils !") == (
        "elements-d-architecture-agents-outils"
    )
    assert slugify("") == "document"


def test_frontmatter_escapes_values_that_would_break_yaml() -> None:
    """Un `:` non échappé casse le bloc, et Obsidian n'affiche plus rien."""
    document = Document(
        title='Rapport : "Q3" — synthèse',
        body="…",
        source="rapport.pdf",
        source_type="pdf",
        tags=("veille", "veille", "agents"),
    )

    rendered = render_frontmatter(document, ingested="2026-08-04")

    assert 'title: "Rapport : \\"Q3\\" — synthèse"' in rendered
    assert "tags: [agents, veille]" in rendered  # dédupliqué et trié
    assert rendered.startswith("---") and rendered.endswith("---")


def test_html_keeps_structure_and_drops_noise() -> None:
    converted = html_to_markdown(PAGE)

    assert converted.title == "Patterns d'agents"
    assert "# Introduction" in converted.body
    assert "- Recherche" in converted.body
    assert "color:red" not in converted.body
    assert "track()" not in converted.body


async def test_markdown_is_kept_as_is_with_its_heading(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Notes de veille\n\nRAG hybride.", encoding="utf-8")

    converted = await convert_file(source)

    assert converted.title == "Notes de veille"
    assert converted.body == "# Notes de veille\n\nRAG hybride."


async def test_unsupported_format_names_what_is_accepted(tmp_path: Path) -> None:
    """Le message part vers l'agent : il doit dire quoi faire, pas seulement refuser."""
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"\xff\xd8\xff")

    with pytest.raises(ConversionError, match="Formats acceptés"):
        await convert_file(source)


async def test_oversized_document_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "gros.txt"
    source.write_text("x" * 5_000_001, encoding="utf-8")

    with pytest.raises(ConversionError, match="volumineux"):
        await convert_file(source)


def test_writing_twice_never_overwrites(library: KnowledgeLibrary) -> None:
    document = Document(title="Même titre", body="Premier", source="a", source_type="text")

    first = library.write(document)
    second = library.write(
        Document(title="Même titre", body="Second", source="b", source_type="text")
    )

    assert first != second
    assert "Premier" in first.read_text(encoding="utf-8")
    assert "Second" in second.read_text(encoding="utf-8")


def test_vocabulary_is_created_and_readable(library: KnowledgeLibrary) -> None:
    assert (library.library / TAGS_FILE).is_file()
    assert "veille" in library.known_tags()


def test_pending_ignores_hidden_files(library: KnowledgeLibrary) -> None:
    (library.incoming / "doc.md").write_text("# A", encoding="utf-8")
    (library.incoming / ".DS_Store").write_bytes(b"\x00")
    (library.incoming / "sous-dossier").mkdir()

    assert [path.name for path in library.pending()] == ["doc.md"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a", True),
        ("http://example.com", True),
        ("/tmp/fichier.pdf", False),
        ("file:///tmp/x", False),
        ("C:\\Users\\doc.pdf", False),
    ],
)
def test_url_detection_separates_sources(value: str, expected: bool) -> None:
    assert is_url(value) is expected


@pytest.mark.parametrize(
    "payload",
    [
        {"markdown": "Contenu", "title": "T"},
        {"content": "Contenu"},
        {"result": {"text": "Contenu"}},
        [{"body": "Contenu"}],
        "Contenu",
    ],
)
def test_scrape_output_is_read_whatever_its_shape(payload: object) -> None:
    """La sortie de ketch est un contrat externe : se tromper de clé donnerait
    un document vide sans que rien ne le signale."""
    _, body = _extract(payload)

    assert body == "Contenu"
