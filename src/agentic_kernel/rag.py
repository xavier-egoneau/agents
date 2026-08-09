from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import pathspec
from pydantic import BaseModel, ConfigDict, Field

from .network_policy import validate_http_target

WORD_RE = re.compile(r"[\wÀ-ÖØ-öø-ÿ]{2,}", re.UNICODE)
EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".codex",
    ".venv",
    "node_modules",
    "__pycache__",
}
SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "providers.json",
    "secrets.json",
}


class RagConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_backend: Literal["feature_hash", "openai_compatible"] = "feature_hash"
    embedding_base_url: str | None = None
    embedding_model: str = "local-feature-hash-v1"
    embedding_dimensions: int = Field(default=384, ge=64, le=4096)
    credential_ref: str | None = None
    allow_private_endpoint: bool = False
    chunk_size_chars: int = Field(default=2400, ge=400, le=20_000)
    chunk_overlap_chars: int = Field(default=300, ge=0, le=4000)
    batch_size: int = Field(default=32, ge=1, le=256)
    min_vector_score: float = Field(default=0.15, ge=-1, le=1)
    max_files: int = Field(default=5000, ge=1, le=100_000)
    max_file_bytes: int = Field(default=1_000_000, ge=1000, le=20_000_000)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    start_line: int
    end_line: int
    content: str


def load_rag_config(content_root: Path) -> RagConfig:
    path = content_root / "rag.json"
    if not path.exists():
        return RagConfig()
    return RagConfig.model_validate_json(path.read_text(encoding="utf-8"))


class RagService:
    def __init__(
        self,
        database: Path,
        workspace: Path,
        config: RagConfig,
        *,
        secret_resolver=None,
    ) -> None:
        self.database = database
        self.workspace = workspace.resolve()
        self.config = config
        self.secret_resolver = secret_resolver

    def _db(self) -> sqlite3.Connection:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                project TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                embedding_fingerprint TEXT NOT NULL DEFAULT '',
                indexed_at TEXT NOT NULL,
                PRIMARY KEY(project, path)
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                path TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                UNIQUE(project, path, ordinal)
            );
            CREATE INDEX IF NOT EXISTS knowledge_chunks_project_path
                ON knowledge_chunks(project, path);
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                chunk_id UNINDEXED,
                project UNINDEXED,
                path UNINDEXED,
                content,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(knowledge_documents)")}
        if "embedding_fingerprint" not in columns:
            db.execute(
                "ALTER TABLE knowledge_documents "
                "ADD COLUMN embedding_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        return db

    async def index(  # noqa: C901 - dette: indexation multi-étapes
        self,
        root: Path,
        extensions: set[str],
    ) -> dict[str, Any]:
        root = root.resolve()
        root.relative_to(self.workspace)
        if not root.exists():
            raise FileNotFoundError(f"knowledge index root does not exist: {root}")
        if not root.is_file() and not root.is_dir():
            raise ValueError(f"knowledge index root is not a file or directory: {root}")
        ignore = self._ignore_spec()
        if root.is_file():
            candidates = [root] if self._is_indexable(root, ignore) else []
        else:
            candidates = sorted(
                (
                    item
                    for item in root.rglob("*")
                    if item.is_file()
                    and item.suffix.casefold() in extensions
                    and self._is_indexable(item, ignore)
                ),
                key=lambda item: str(item).casefold(),
            )
        candidates = candidates[: self.config.max_files]
        project = str(self.workspace)
        discovered: set[str] = set()
        changed_documents = skipped = unchanged = chunks_written = 0
        embedding_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "backend": self.config.embedding_backend,
                    "model": self.config.embedding_model,
                    "dimensions": self.config.embedding_dimensions,
                    "base_url": self.config.embedding_base_url,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self._db() as db:
            existing = {
                row["path"]: (row["sha256"], row["embedding_fingerprint"])
                for row in db.execute(
                    """SELECT path, sha256, embedding_fingerprint
                       FROM knowledge_documents WHERE project=?""",
                    (project,),
                )
            }
        pending: list[tuple[str, bytes, list[Chunk]]] = []
        for candidate in candidates:
            try:
                relative = str(candidate.relative_to(self.workspace))
                raw = candidate.read_bytes()
                if len(raw) > self.config.max_file_bytes:
                    skipped += 1
                    continue
                content = raw.decode("utf-8")
            except (OSError, UnicodeError, ValueError):
                skipped += 1
                continue
            discovered.add(relative)
            digest = hashlib.sha256(raw).hexdigest()
            if existing.get(relative) == (digest, embedding_fingerprint):
                unchanged += 1
                continue
            pending.append((relative, raw, chunk_text(content, self.config)))

        for path, raw, chunks in pending:
            vectors: list[list[float]] = []
            for offset in range(0, len(chunks), self.config.batch_size):
                batch = chunks[offset : offset + self.config.batch_size]
                vectors.extend(await self.embed([chunk.content for chunk in batch]))
            values = list(zip(chunks, vectors, strict=True))
            with self._db() as db:
                db.execute(
                    "DELETE FROM knowledge_fts WHERE project=? AND path=?",
                    (project, path),
                )
                db.execute(
                    "DELETE FROM knowledge_chunks WHERE project=? AND path=?",
                    (project, path),
                )
                now = datetime.now(UTC).isoformat()
                digest = hashlib.sha256(raw).hexdigest()
                db.execute(
                    """INSERT INTO knowledge_documents
                       (project, path, sha256, bytes, embedding_fingerprint, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project, path) DO UPDATE SET
                         sha256=excluded.sha256, bytes=excluded.bytes,
                         embedding_fingerprint=excluded.embedding_fingerprint,
                         indexed_at=excluded.indexed_at""",
                    (
                        project,
                        path,
                        digest,
                        len(raw),
                        embedding_fingerprint,
                        now,
                    ),
                )
                for chunk, vector in values:
                    chunk_id = hashlib.sha256(
                        f"{project}\0{path}\0{chunk.ordinal}\0{digest}".encode()
                    ).hexdigest()
                    content_hash = hashlib.sha256(chunk.content.encode()).hexdigest()
                    db.execute(
                        """INSERT INTO knowledge_chunks
                           (chunk_id, project, path, ordinal, start_line, end_line,
                            content, content_hash, embedding_json, embedding_model,
                            indexed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            chunk_id,
                            project,
                            path,
                            chunk.ordinal,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.content,
                            content_hash,
                            json.dumps(vector, separators=(",", ":")),
                            self.config.embedding_model,
                            now,
                        ),
                    )
                    db.execute(
                        """INSERT INTO knowledge_fts
                           (chunk_id, project, path, content) VALUES (?, ?, ?, ?)""",
                        (chunk_id, project, path, chunk.content),
                    )
                    chunks_written += 1
                changed_documents += 1

        prefix = "" if root == self.workspace else str(root.relative_to(self.workspace))
        with self._db() as db:
            indexed_paths = {
                row["path"]
                for row in db.execute(
                    "SELECT path FROM knowledge_documents WHERE project=?",
                    (project,),
                )
                if not prefix
                or row["path"] == prefix
                or row["path"].startswith(prefix.rstrip("/") + "/")
            }
            removed = sorted(indexed_paths - discovered)
            for path in removed:
                db.execute(
                    "DELETE FROM knowledge_fts WHERE project=? AND path=?",
                    (project, path),
                )
                db.execute(
                    "DELETE FROM knowledge_chunks WHERE project=? AND path=?",
                    (project, path),
                )
                db.execute(
                    "DELETE FROM knowledge_documents WHERE project=? AND path=?",
                    (project, path),
                )
        return {
            "indexed": changed_documents,
            "documents_indexed": changed_documents,
            "documents_unchanged": unchanged,
            "documents_removed": len(removed),
            "chunks_written": chunks_written,
            "skipped": skipped,
            "embedding_backend": self.config.embedding_backend,
            "embedding_model": self.config.embedding_model,
        }

    def _ignore_spec(self) -> pathspec.PathSpec | None:
        path = self.workspace / ".gitignore"
        try:
            return pathspec.GitIgnoreSpec.from_lines(path.read_text(encoding="utf-8").splitlines())
        except OSError:
            return None

    def _is_indexable(
        self,
        candidate: Path,
        ignore: pathspec.PathSpec | None,
    ) -> bool:
        if candidate.is_symlink():
            return False
        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError:
            return False
        if set(relative.parts) & EXCLUDED_PARTS:
            return False
        if relative.name in SENSITIVE_FILENAMES or relative.name.startswith(".env."):
            return False
        if relative.parts[:2] == ("content-agents", "sessions"):
            return False
        return not (ignore and ignore.match_file(relative.as_posix()))

    async def search(self, query: str, limit: int) -> dict[str, Any]:
        terms = tokenize(query)
        if not terms:
            return {"results": [], "query": query}
        query_vector, degraded = await self._query_vector(query)
        project = str(self.workspace)
        fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])
        with self._db() as db:
            lexical_rows = db.execute(
                """SELECT chunk_id, path, content, bm25(knowledge_fts) AS score
                   FROM knowledge_fts
                   WHERE knowledge_fts MATCH ? AND project=?
                   ORDER BY score LIMIT ?""",
                (fts_query, project, max(limit * 5, 25)),
            ).fetchall()
            vector_rows = db.execute(
                """SELECT chunk_id, path, content, start_line, end_line,
                          embedding_json
                   FROM knowledge_chunks WHERE project=?""",
                (project,),
            ).fetchall()
            coordinates = {
                row["chunk_id"]: (row["start_line"], row["end_line"]) for row in vector_rows
            }
        if query_vector is None:
            # Sans vecteur de requête, la comparaison sémantique n'a pas de
            # sens; la recherche lexicale, elle, n'a jamais eu besoin du
            # service d'embeddings.
            vector_ranked: list[tuple[float, Any]] = []
        else:
            vector_ranked = sorted(
                (
                    (
                        cosine(query_vector, json.loads(row["embedding_json"])),
                        row,
                    )
                    for row in vector_rows
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            vector_ranked = [
                item for item in vector_ranked if item[0] >= self.config.min_vector_score
            ][: max(limit * 5, 25)]
        scores: dict[str, float] = {}
        evidence: dict[str, tuple[str, str, float, float]] = {}
        for rank, row in enumerate(lexical_rows, 1):
            chunk_id = row["chunk_id"]
            lexical = 1.0 / (1.0 + max(0.0, float(row["score"])))
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            evidence[chunk_id] = (row["path"], row["content"], lexical, 0.0)
        for rank, (similarity, row) in enumerate(vector_ranked, 1):
            chunk_id = row["chunk_id"]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            previous = evidence.get(chunk_id, (row["path"], row["content"], 0.0, 0.0))
            evidence[chunk_id] = (previous[0], previous[1], previous[2], similarity)

        def rerank(item: tuple[str, float]) -> tuple[float, float]:
            chunk_id, reciprocal_rank = item
            content = evidence[chunk_id][1].casefold()
            coverage = sum(term.casefold() in content for term in terms) / len(terms)
            return reciprocal_rank + coverage * 0.02, evidence[chunk_id][3]

        selected = sorted(scores.items(), key=rerank, reverse=True)[:limit]
        results = []
        for chunk_id, fused_score in selected:
            path, content, lexical_score, vector_score = evidence[chunk_id]
            start_line, end_line = coordinates[chunk_id]
            results.append(
                {
                    "chunk_id": chunk_id,
                    "path": path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "citation": f"{path}#L{start_line}-L{end_line}",
                    "excerpt": bounded_excerpt(content, terms),
                    "scores": {
                        "fused": round(fused_score, 6),
                        "lexical": round(lexical_score, 6),
                        "vector": round(vector_score, 6),
                    },
                }
            )
        return {
            "query": query,
            "results": results,
            "backend": "fts5_only" if query_vector is None else "hybrid_fts5_vector_rrf",
            "embedding_backend": self.config.embedding_backend,
            "embedding_model": self.config.embedding_model,
            # Nommer la dégradation : des résultats purement lexicaux présentés
            # comme hybrides laisseraient croire qu'un sujet est absent alors
            # qu'il n'a simplement pas été trouvé par les mots employés.
            "degraded": degraded,
        }

    async def _query_vector(self, query: str) -> tuple[list[float] | None, str | None]:
        """Vecteur de la requête, ou None si le service d'embeddings est absent.

        L'appel était en première ligne de `search` : une erreur de connexion
        emportait la recherche entière, y compris la partie FTS5 qui ne dépend
        d'aucun service. Une bibliothèque devenait inutilisable faute d'un
        serveur local lancé — alors que le lexical seul rend déjà service.
        """
        try:
            return (await self.embed([query]))[0], None
        except Exception as exc:  # toute panne du service vaut repli
            return None, f"{type(exc).__name__}: {exc}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.config.embedding_backend == "feature_hash":
            return [
                feature_hash_embedding(text, self.config.embedding_dimensions) for text in texts
            ]
        if not self.config.embedding_base_url:
            raise ValueError("embedding_base_url is required for openai_compatible embeddings")
        endpoint = self.config.embedding_base_url.rstrip("/")
        if not endpoint.endswith("/embeddings"):
            endpoint += "/embeddings"
        await validate_http_target(
            endpoint,
            allow_private=self.config.allow_private_endpoint,
        )
        headers: dict[str, str] = {}
        if self.config.credential_ref:
            token = (
                self.secret_resolver(self.config.credential_ref)
                if callable(self.secret_resolver)
                else None
            )
            if not token:
                raise ValueError(f"credential reference unavailable: {self.config.credential_ref}")
            headers["authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                endpoint,
                headers=headers,
                json={"model": self.config.embedding_model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()
        ordered = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [normalize([float(value) for value in item["embedding"]]) for item in ordered]
        if len(vectors) != len(texts):
            raise ValueError("embedding endpoint returned an unexpected vector count")
        return vectors


def chunk_text(content: str, config: RagConfig) -> list[Chunk]:
    lines = content.splitlines()
    if not lines:
        return [Chunk(0, 1, 1, "")]
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines):
            addition = len(lines[end]) + 1
            if end > start and size + addition > config.chunk_size_chars:
                break
            size += addition
            end += 1
        text = "\n".join(lines[start:end]).strip()
        if text:
            chunks.append(Chunk(len(chunks), start + 1, end, text))
        if end >= len(lines):
            break
        overlap = 0
        next_start = end
        while next_start > start and overlap < config.chunk_overlap_chars:
            next_start -= 1
            overlap += len(lines[next_start]) + 1
        start = max(start + 1, next_start)
    return chunks


def tokenize(value: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).casefold() for match in WORD_RE.finditer(value)))


def feature_hash_embedding(value: str, dimensions: int) -> list[float]:
    tokens = tokenize(value)
    features = [
        *tokens,
        *(f"{left}::{right}" for left, right in zip(tokens, tokens[1:], strict=False)),
    ]
    vector = [0.0] * dimensions
    for feature in features:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % dimensions
        sign = 1.0 if digest[0] & 1 else -1.0
        vector[index] += sign
    return normalize(vector)


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def bounded_excerpt(content: str, terms: list[str], limit: int = 1600) -> str:
    folded = content.casefold()
    positions = [folded.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - 300)
    return content[start : start + limit]
