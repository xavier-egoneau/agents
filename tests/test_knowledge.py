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


def test_reingesting_a_subject_rewrites_its_page(library: KnowledgeLibrary) -> None:
    """Une encyclopédie, pas un journal de bord.

    Suffixer produisait `sujet.md`, `sujet-2.md`, `sujet-3.md` : la recherche
    renvoyait trois états du même sujet sans dire lequel faisait foi.
    """
    first = library.write(
        Document(title="Même titre", body="Premier", source="a", source_type="text")
    )
    second = library.write(
        Document(title="Même titre", body="Second", source="b", source_type="text")
    )

    assert first == second
    assert "Second" in second.read_text(encoding="utf-8")
    assert "Premier" not in second.read_text(encoding="utf-8")
    assert [p.name for p in library.library.glob("*.md") if p.name != TAGS_FILE] == [
        "meme-titre.md"
    ]


def test_the_replaced_version_survives_in_the_journal(library: KnowledgeLibrary) -> None:
    """La réécriture est confiée au modèle : sans filet, elle perd en silence.

    La bibliothèque n'étant pas versionnée, le journal est le seul recours.
    """
    library.write(Document(title="Sujet", body="Première rédaction", source="a", source_type="text"))
    library.write(Document(title="Sujet", body="Seconde rédaction", source="b", source_type="text"))

    journal = library.journal_path().read_text(encoding="utf-8")

    assert "Première rédaction" in journal
    assert journal.count("## ") == 2  # une entrée par ingestion, rien d'effacé


def test_the_journal_stays_out_of_the_library(library: KnowledgeLibrary) -> None:
    """Indexer le journal ferait remonter les traces datées et les versions
    périmées — exactement le bruit que la page par sujet supprime."""
    library.write(Document(title="Sujet", body="Contenu", source="a", source_type="text"))

    assert library.journal.is_dir()
    assert not list(library.library.glob("**/2*.md"))
    assert library.journal not in library.library.parents


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


async def test_indexing_targets_the_library_only(library: KnowledgeLibrary) -> None:
    """Le journal doit rester hors de l'index.

    L'indexation reçoit `library.library`, pas la racine. Ce test fixe ce
    contrat : élargir la portée à `library.root` ferait entrer les versions
    remplacées dans les résultats, et la recherche redeviendrait un journal.
    """
    library.write(Document(title="Sujet", body="Contenu", source="a", source_type="text"))
    library.write(Document(title="Sujet", body="Révisé", source="b", source_type="text"))

    indexable = sorted(path.name for path in library.library.rglob("*.md"))

    assert indexable == [TAGS_FILE, "sujet.md"]
    # Le journal existe, avec la version remplacée, mais ailleurs.
    assert "Contenu" in library.journal_path().read_text(encoding="utf-8")


def test_the_save_command_carries_its_capture_instruction(tmp_path: Path) -> None:
    """`/save` doit imposer l'archivage, pas seulement charger la skill.

    Charger `knowledge-capture` ne suffirait pas : l'outil d'ingestion est déjà
    disponible sans elle, et n'a jamais été appelé pour autant.
    """
    from agentic_kernel.config import ProjectConfig

    racine = tmp_path / "content-agents" / "skills" / "knowledge-capture"
    racine.mkdir(parents=True)
    # La source de vérité est le socle livré : `content-agents/` appartient à
    # l'utilisateur, vit hors du dépôt et peut être déplacé.
    source = (
        Path(__file__).parents[1]
        / "src" / "agentic_kernel" / "defaults"
        / "skills" / "knowledge-capture" / "SKILL.md"
    )
    (racine / "SKILL.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "content-agents" / "providers.json").write_text(
        '{"version": 1, "default_provider": "test",'
        ' "providers": [{"id": "test", "kind": "deepseek", "connection_type": "api_key",'
        ' "model": "m", "api_key": "x"}]}',
        encoding="utf-8",
    )

    commande = ProjectConfig(tmp_path).resolve_command("/save cherche les tardigrades")

    assert commande is not None
    assert commande["skill"] == "knowledge-capture"
    assert "knowledge_ingest" in commande["prompt"]
    # Le titre nomme un sujet : sans cette consigne, la page devient datée et
    # rien ne viendra jamais la mettre à jour.
    assert "sujet" in commande["prompt"]


def _library_for(tmp_path: Path, orchestrateur: str | None):
    """Résout la bibliothèque comme le fait le module, sans monter un run."""
    import importlib.util
    from types import SimpleNamespace

    source = Path(__file__).parents[1] / "tools/modules/memory/module.py"
    spec = importlib.util.spec_from_file_location("test_memory_module", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contenu = tmp_path / "content-agents"
    contenu.mkdir(parents=True, exist_ok=True)
    deps = SimpleNamespace(
        state_db=contenu / "state.db",
        events=SimpleNamespace(directory=contenu / "sessions"),
        orchestrator_id=orchestrateur,
    )
    return module._library(SimpleNamespace(deps=deps))


def test_each_orchestrator_owns_its_library(tmp_path: Path) -> None:
    """Deux orchestrateurs ne partagent rien : `main` fait du développement,
    `sophie` de l'accompagnement — mêler leurs corpus n'aurait aucun sens."""
    main = _library_for(tmp_path, "main")
    sophie = _library_for(tmp_path, "sophie")

    assert main.library != sophie.library
    assert main.library.parent.parent.name == "main"
    assert sophie.library.parent.parent.name == "sophie"


def test_a_sub_agent_inherits_the_library_of_its_orchestrator(tmp_path: Path) -> None:
    """`orchestrator_id` porte la racine du run, pas l'agent courant.

    Sans cela, ce qu'un sous-agent archive disparaîtrait à la fin de la
    délégation, dans une bibliothèque que personne ne rouvrirait.
    """
    depuis_main = _library_for(tmp_path, "main")
    # `dev` invoqué par `main` reçoit le même `orchestrator_id`.
    depuis_dev = _library_for(tmp_path, "main")

    assert depuis_dev.library == depuis_main.library


def test_an_unusable_identifier_falls_back_to_the_shared_folder(tmp_path: Path) -> None:
    """Un identifiant hors format ne doit pas servir à bâtir un chemin."""
    for suspect in (None, "", "../evasion", "Main Agent"):
        resolue = _library_for(tmp_path, suspect)
        assert resolue.root.name == "knowledge"
        assert "workspaces" not in resolue.root.parts


def _instruction(library: KnowledgeLibrary, mode: str, pages=()):
    from agentic_kernel.kernel import _knowledge_instruction

    return _knowledge_instruction(library, mode, list(pages))


def test_nothing_is_injected_by_default(library: KnowledgeLibrary) -> None:
    """La bibliothèque n'entre jamais dans le contexte d'elle-même."""
    library.write(Document(title="Tardigrades", body="Contenu", source="a", source_type="text"))

    assert _instruction(library, "off") == ""


def test_auto_mode_supplies_the_index_not_the_pages(library: KnowledgeLibrary) -> None:
    """L'agent disposait déjà de la recherche mais ne s'en servait pas :
    il ignorait que la bibliothèque contenait quoi que ce soit."""
    library.write(
        Document(
            title="Tardigrades",
            body="Un contenu long qui n'a pas à occuper le contexte.",
            source="a",
            source_type="text",
            tags=("veille",),
        )
    )

    instruction = _instruction(library, "auto")

    assert "Tardigrades" in instruction
    assert "Un contenu long" not in instruction
    assert "knowledge_search" in instruction
    # Ni slug ni tags : `knowledge_search` prend une requête libre, et cet index
    # est payé à chaque message — tout ce qui n'aide pas à décider est du poids.
    assert "`tardigrades`" not in instruction
    assert "veille" not in instruction


def test_manual_mode_supplies_the_selected_pages(library: KnowledgeLibrary) -> None:
    library.write(Document(title="Retenue", body="Texte attendu", source="a", source_type="text"))
    library.write(Document(title="Ignorée", body="Texte écarté", source="b", source_type="text"))

    instruction = _instruction(library, "manual", ["retenue"])

    assert "Texte attendu" in instruction
    assert "Texte écarté" not in instruction


def test_an_unknown_page_is_skipped_rather_than_fatal(library: KnowledgeLibrary) -> None:
    """La sélection vient de la surface et peut désigner une page supprimée
    depuis; perdre le run pour autant serait disproportionné."""
    library.write(Document(title="Présente", body="Texte", source="a", source_type="text"))

    instruction = _instruction(library, "manual", ["presente", "disparue", "../evasion"])

    assert "Texte" in instruction


def test_an_empty_library_injects_nothing(library: KnowledgeLibrary) -> None:
    assert _instruction(library, "auto") == ""
    assert _instruction(library, "manual", ["quoi-que-ce-soit"]) == ""
