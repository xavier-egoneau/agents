import json
from pathlib import Path

import httpx

from agentic_kernel.rag import RagConfig, RagService, chunk_text


async def test_hybrid_rag_indexes_chunks_cites_lines_and_removes_stale_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / "architecture.md"
    source.write_text(
        "\n".join(
            [
                "# Architecture",
                "Le guardian autorise ou suspend chaque action contrôlée.",
                "Les événements JSONL constituent la source d’audit.",
                "SQLite est uniquement une projection reconstruisible.",
            ]
            * 35
        ),
        encoding="utf-8",
    )
    service = RagService(
        tmp_path / "state.db",
        workspace,
        RagConfig(
            chunk_size_chars=500,
            chunk_overlap_chars=80,
            embedding_dimensions=128,
        ),
    )

    indexed = await service.index(workspace, {".md"})
    assert indexed["documents_indexed"] == 1
    assert indexed["chunks_written"] > 1
    unchanged = await service.index(workspace, {".md"})
    assert unchanged["documents_unchanged"] == 1

    result = await service.search("source audit JSONL", 3)
    assert result["backend"] == "hybrid_fts5_vector_rrf"
    assert result["results"]
    first = result["results"][0]
    assert first["path"] == "architecture.md"
    assert first["citation"].startswith("architecture.md#L")
    assert first["start_line"] <= first["end_line"]
    assert set(first["scores"]) == {"fused", "lexical", "vector"}

    source.unlink()
    removed = await service.index(workspace, {".md"})
    assert removed["documents_removed"] == 1
    assert (await service.search("audit JSONL", 3))["results"] == []


async def test_rag_never_indexes_secrets_or_ignored_files(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (workspace / "ignored.md").write_text("DO_NOT_INDEX_IGNORED", encoding="utf-8")
    content = workspace / "content-agents"
    content.mkdir()
    (content / "providers.json").write_text(
        '{"api_key":"DO_NOT_INDEX_SECRET"}',
        encoding="utf-8",
    )
    (workspace / "public.md").write_text("PUBLIC_KNOWLEDGE", encoding="utf-8")
    service = RagService(tmp_path / "state.db", workspace, RagConfig())
    indexed = await service.index(workspace, {".md", ".json"})
    assert indexed["documents_indexed"] == 1
    assert (await service.search("PUBLIC_KNOWLEDGE", 5))["results"]
    assert (await service.search("DO_NOT_INDEX_SECRET", 5))["results"] == []
    assert (await service.search("DO_NOT_INDEX_IGNORED", 5))["results"] == []


async def test_openai_compatible_embedding_backend_is_configurable(
    tmp_path: Path,
    httpx_mock,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    def embeddings(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        data = [
            {"index": index, "embedding": [1.0, float(index + 1), 0.5]}
            for index, _ in enumerate(payload["input"])
        ]
        return httpx.Response(200, json={"data": data})

    httpx_mock.add_callback(
        embeddings,
        method="POST",
        url="https://example.com/v1/embeddings",
    )
    service = RagService(
        tmp_path / "state.db",
        workspace,
        RagConfig(
            embedding_backend="openai_compatible",
            embedding_base_url="https://example.com/v1",
            embedding_model="embedding-test",
            embedding_dimensions=64,
        ),
    )
    vectors = await service.embed(["alpha", "beta"])
    assert len(vectors) == 2
    assert all(abs(sum(value * value for value in vector) - 1.0) < 1e-6 for vector in vectors)


def test_chunking_preserves_line_coordinates_and_overlap() -> None:
    chunks = chunk_text(
        "\n".join(f"line {index}: {'x' * 40}" for index in range(1, 31)),
        RagConfig(chunk_size_chars=400, chunk_overlap_chars=100),
    )
    assert len(chunks) > 1
    assert chunks[0].start_line == 1
    assert chunks[1].start_line <= chunks[0].end_line
    assert chunks[-1].end_line == 30
