"""Lecture seule de l'arborescence d'un workspace, pour l'explorateur de la surface web.

Le routeur ne sert qu'a *lister* et *lire* : aucune ecriture, aucune suppression.
Toute resolution de chemin est confinee sous la racine du workspace demande.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Repertoires jamais parcourus : bruit et volume, sans interet pour la revue.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".turbo",
        "dist",
        "build",
        ".wrangler",
        ".DS_Store",
    }
)

MAX_ENTRIES = 1_000
MAX_PREVIEW_BYTES = 400_000

TEXT_SUFFIXES = frozenset(
    {
        ".md", ".txt", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        ".json", ".yaml", ".yml", ".toml", ".css", ".scss", ".html", ".svg",
        ".sh", ".sql", ".ini", ".cfg", ".env", ".lock", ".rs", ".go", ".java",
    }
)


class FileNode(BaseModel):
    name: str
    path: str
    kind: str  # "dir" | "file"
    size: int | None = None


def create_files_router() -> APIRouter:
    router = APIRouter(prefix="/api/files", tags=["files"])

    @router.get("/tree")
    async def tree(workspace: str, path: str = "") -> dict[str, object]:
        """Liste un seul niveau. L'arbre se construit par expansions successives."""
        root = _root(workspace)
        target = _resolve(root, path)
        if not target.is_dir():
            raise HTTPException(status_code=422, detail="Le chemin n'est pas un dossier.")

        nodes: list[FileNode] = []
        truncated = False
        try:
            entries = sorted(
                target.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Dossier illisible.") from exc

        for entry in entries:
            if entry.name in IGNORED_DIRS or entry.name.startswith("~"):
                continue
            if len(nodes) >= MAX_ENTRIES:
                truncated = True
                break
            is_dir = entry.is_dir()
            nodes.append(
                FileNode(
                    name=entry.name,
                    path=entry.relative_to(root).as_posix(),
                    kind="dir" if is_dir else "file",
                    size=None if is_dir else _safe_size(entry),
                )
            )

        return {
            "path": target.relative_to(root).as_posix() if target != root else "",
            "entries": [node.model_dump() for node in nodes],
            "truncated": truncated,
        }

    @router.get("/content")
    async def content(workspace: str, path: str) -> dict[str, object]:
        """Renvoie le contenu texte d'un fichier, tronque si necessaire."""
        root = _root(workspace)
        target = _resolve(root, path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Fichier introuvable.")

        size = _safe_size(target) or 0
        if target.suffix.lower() not in TEXT_SUFFIXES and size > 64_000:
            raise HTTPException(status_code=415, detail="Apercu texte indisponible pour ce fichier.")

        raw = target.read_bytes()[:MAX_PREVIEW_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=415, detail="Fichier binaire.") from None

        return {
            "path": target.relative_to(root).as_posix(),
            "content": text,
            "truncated": size > MAX_PREVIEW_BYTES,
            "size": size,
        }

    return router


def _root(workspace: str) -> Path:
    root = Path(workspace).expanduser()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail="Workspace introuvable.") from exc
    if not root.is_dir():
        raise HTTPException(status_code=422, detail="Workspace introuvable.")
    return root


def _resolve(root: Path, relative: str) -> Path:
    """Empeche toute evasion hors du workspace (../, liens symboliques inclus)."""
    candidate = (root / relative).resolve() if relative else root
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=403, detail="Chemin hors du workspace.")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="Chemin introuvable.")
    return candidate


def _safe_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None
