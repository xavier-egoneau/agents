"""Bibliothèque de connaissance et recherche documentaire.

Deux corpus distincts, d'où le paramètre `scope` :

- `project` — les fichiers du workspace courant, dont `DECISION.md` et
  `MEMORY.md`. Cette connaissance appartient au dépôt et voyage avec lui.
- `library` — la bibliothèque de l'orchestrateur qui a lancé le run, sous
  `content-agents/workspaces/<orchestrateur>/knowledge/library/`. Elle lui
  appartient : deux orchestrateurs ne partagent rien, et un sous-agent hérite
  de celle de son parent le temps de la délégation.

Sans ce paramètre, ni l'agent ni le lecteur de ses citations ne saurait lequel
des deux a répondu.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.converters import ConversionError, convert_file
from agentic_kernel.knowledge import Document, KnowledgeLibrary
from agentic_kernel.rag import RagService, load_rag_config
from agentic_kernel.web_capture import is_url, scrape_to_markdown

Scope = Literal["project", "library"]

DEFAULT_EXTENSIONS = [
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".json", ".toml", ".yaml", ".yml",
]


_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _library(ctx: RunContext[Any]) -> KnowledgeLibrary:
    """Bibliothèque de l'orchestrateur qui a lancé le run.

    Elle appartient à l'agent racine, pas à l'agent courant : un sous-agent
    appelé par `main` lit et écrit dans celle de `main`, sinon ce qu'il apprend
    disparaîtrait à la fin de la délégation.

    Un identifiant hors format retomberait sur le dossier partagé plutôt que de
    construire un chemin à partir d'une chaîne arbitraire.
    """
    state_db = Path(ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db"))
    contenu = state_db.parent
    orchestrateur = str(getattr(ctx.deps, "orchestrator_id", "") or "")
    if not _AGENT_ID.fullmatch(orchestrateur):
        return KnowledgeLibrary(contenu / "knowledge")
    return KnowledgeLibrary(contenu / "workspaces" / orchestrateur / "knowledge")


def _rag(ctx: RunContext[Any], scope: Scope) -> RagService:
    """Un service par corpus : `project` dans RagService vaut la racine indexée."""
    state_db = Path(ctx.deps.state_db or (ctx.deps.events.directory.parent / "state.db"))
    root = Path(ctx.deps.workspace) if scope == "project" else _library(ctx).library
    return RagService(
        state_db,
        root,
        load_rag_config(state_db.parent),
        secret_resolver=getattr(ctx.deps, "secret_resolver", None),
    )


def _failure(message: str, kind: str = "validation") -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"type": kind, "message": message}, "metadata": {}}


async def knowledge_ingest(
    ctx: RunContext[Any],
    source: str,
    tags: list[str] | None = None,
    summary: str = "",
    title: str = "",
    justification: str = "",
) -> dict[str, Any]:
    """Convert a URL or local document to markdown and file it in the library.

    Accepts an http(s) URL, or a path to a PDF, HTML, markdown or text file.
    Use tags from the library vocabulary listed in library/_tags.md.

    The library is an encyclopedia: one page per subject. Reusing a `title`
    rewrites that page instead of adding a second one, so choose the subject
    name rather than an event name — "Autonomous agent patterns", never
    "Watch, 8 August". Before rewriting an existing page, read it and keep what
    is still true: the previous version is archived in the journal, but nothing
    replays it for you.
    """
    library = _library(ctx)
    library.ensure()
    try:
        if is_url(source):
            converted = await scrape_to_markdown(source)
        else:
            candidate = Path(source)
            if not candidate.is_absolute():
                candidate = (library.incoming / source).resolve()
            converted = await convert_file(candidate)
    except ConversionError as exc:
        return _failure(str(exc), "precondition")

    known = library.known_tags()
    requested = [tag.strip() for tag in (tags or []) if tag.strip()]
    unknown = sorted(set(requested) - known)
    document = Document(
        title=title.strip() or converted.title or "Sans titre",
        body=converted.body,
        source=source,
        source_type=converted.source_type,
        tags=tuple(tag for tag in requested if tag in known),
        summary=summary.strip(),
    )
    written = library.write(document)
    indexed = await _rag(ctx, "library").index(library.library, {".md"})
    return {
        "ok": True,
        "data": {
            "path": str(written),
            "title": document.title,
            "tags": list(document.tags),
            "chunks_written": indexed.get("chunks_written"),
        },
        "error": None,
        # Un tag inconnu est écarté plutôt qu'accepté en silence : c'est ce qui
        # empêche la taxonomie de dériver au fil des ingestions.
        "metadata": {
            "rejected_tags": unknown,
            "known_tags": sorted(known),
        },
    }


async def knowledge_sync(ctx: RunContext[Any], justification: str = "") -> dict[str, Any]:
    """Convert every document waiting in the library incoming/ folder, then index."""
    library = _library(ctx)
    library.ensure()
    pending = library.pending()
    if not pending:
        return {
            "ok": True,
            "data": {"ingested": [], "skipped": []},
            "error": None,
            "metadata": {"incoming": str(library.incoming)},
        }
    ingested: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for path in pending:
        try:
            converted = await convert_file(path)
        except ConversionError as exc:
            skipped.append({"file": path.name, "reason": str(exc)})
            continue
        written = library.write(
            Document(
                title=converted.title or path.stem,
                body=converted.body,
                source=path.name,
                source_type=converted.source_type,
            )
        )
        # Le fichier d'origine n'est retiré qu'après écriture réussie : une
        # conversion interrompue laisse le document déposé intact.
        path.unlink(missing_ok=True)
        ingested.append({"file": path.name, "path": str(written)})
    indexed = await _rag(ctx, "library").index(library.library, {".md"})
    return {
        "ok": True,
        "data": {"ingested": ingested, "skipped": skipped},
        "error": None,
        "metadata": {"chunks_written": indexed.get("chunks_written")},
    }


async def knowledge_index(
    ctx: RunContext[Any],
    scope: Scope = "project",
    path: str = ".",
    extensions: list[str] | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Incrementally chunk and embed bounded UTF-8 files for the given scope."""
    if scope == "library":
        library = _library(ctx)
        library.ensure()
        data = await _rag(ctx, scope).index(library.library, {".md"})
    else:
        root = (ctx.deps.workspace / path).resolve()
        data = await _rag(ctx, scope).index(root, set(extensions or DEFAULT_EXTENSIONS))
    return {
        "ok": True,
        "data": data,
        "error": None,
        "metadata": {"scope": scope, "backend": "hybrid_fts5_vector", "incremental": True},
    }


async def knowledge_search(
    ctx: RunContext[Any],
    query: str,
    scope: Scope = "project",
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    justification: str = "",
) -> dict[str, Any]:
    """Run hybrid retrieval with line-addressable citations, in one scope."""
    data = await _rag(ctx, scope).search(query, limit)
    return {
        "ok": True,
        "data": data["results"],
        "error": None,
        "metadata": {
            "scope": scope,
            "count": len(data["results"]),
            "backend": data["backend"],
            "embedding_backend": data["embedding_backend"],
            "embedding_model": data["embedding_model"],
            "query": data["query"],
        },
    }


class MemoryModule:
    def toolsets(self):
        return [
            FunctionToolset(
                tools=[knowledge_ingest, knowledge_sync, knowledge_index, knowledge_search]
            )
        ]

    def instructions(self):
        return [
            "Durable project knowledge lives in versioned markdown at the workspace root: "
            "DECISION.md for the decision log, MEMORY.md for the current state. Read them "
            "before exploring unfamiliar code, and update them in the same change that makes "
            "them stale.\n"
            "The library is a separate, machine-local corpus of ingested documents. Use "
            "knowledge_ingest to file a URL or document into it, knowledge_sync to process "
            "what the user dropped in content-agents/knowledge/incoming/, and knowledge_search "
            "with scope='library' to query it. Search scope='project' for workspace files. "
            "Only use tags already listed in library/_tags.md; add one there before using it."
        ]

    def capabilities(self):
        return []


module = MemoryModule()
