from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field


class GitError(RuntimeError):
    pass


class GitFile(BaseModel):
    path: str
    status: str
    staged: bool = False
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    patch: str = ""
    fingerprint: str = ""


class GitSnapshot(BaseModel):
    available: bool
    workspace: str
    repo_root: str | None = None
    branch: str | None = None
    head: str | None = None
    files: list[GitFile] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    fingerprint: str = ""


class GitService:
    """Read and mutate only the repository containing an explicit workspace."""

    def __init__(self, *, patch_limit: int = 400_000) -> None:
        self.patch_limit = patch_limit

    def snapshot(self, workspace: Path | str, *, include_patches: bool = True) -> GitSnapshot:
        selected = self._workspace(workspace)
        root = self._repo_root(selected)
        if root is None:
            return GitSnapshot(available=False, workspace=str(selected))

        branch = self._run(root, "branch", "--show-current", check=False).strip() or "HEAD détachée"
        head = self._run(root, "rev-parse", "--short", "HEAD", check=False).strip() or None
        records = self._status(root)
        files: list[GitFile] = []
        patch_budget = self.patch_limit
        for xy, relative in records:
            patch = (
                self._patch(root, relative, untracked=xy == "??")[:patch_budget]
                if include_patches and patch_budget > 0
                else ""
            )
            patch_budget -= len(patch)
            additions, deletions, binary = self._counts(patch)
            files.append(
                GitFile(
                    path=relative,
                    status=self._label(xy),
                    staged=xy[0] not in {" ", "?"},
                    additions=additions,
                    deletions=deletions,
                    binary=binary,
                    patch=patch,
                    fingerprint=self._file_fingerprint(root, xy, relative),
                )
            )
        return GitSnapshot(
            available=True,
            workspace=str(selected),
            repo_root=str(root),
            branch=branch,
            head=head,
            files=files,
            additions=sum(item.additions for item in files),
            deletions=sum(item.deletions for item in files),
            fingerprint=self._fingerprint(files),
        )

    def branches(self, workspace: Path | str) -> tuple[str, list[str]]:
        root = self.require_repo(workspace)
        current = self._run(root, "branch", "--show-current", check=False).strip()
        names = [
            line.strip()
            for line in self._run(
                root, "for-each-ref", "--format=%(refname:short)", "refs/heads"
            ).splitlines()
            if line.strip()
        ]
        return current, names

    def switch(self, workspace: Path | str, branch: str) -> GitSnapshot:
        root = self.require_repo(workspace)
        if self._status(root):
            raise GitError("Valide ou range les modifications avant de changer de branche.")
        if branch not in self.branches(root)[1]:
            raise GitError("Branche locale introuvable.")
        self._run(root, "switch", branch)
        return self.snapshot(workspace, include_patches=False)

    def commit(self, workspace: Path | str, message: str, fingerprint: str) -> GitSnapshot:
        root = self.require_repo(workspace)
        current = self.snapshot(workspace)
        if not current.files:
            raise GitError("Aucune modification à valider.")
        if not fingerprint or current.fingerprint != fingerprint:
            raise GitError("Le dépôt a changé depuis la proposition. Génère un nouveau message.")
        clean_message = message.strip()
        if not clean_message:
            raise GitError("Le message de commit est vide.")
        index = Path(self._run(root, "rev-parse", "--git-path", "index").strip())
        if not index.is_absolute():
            index = root / index
        backup_dir = Path(tempfile.mkdtemp(prefix="amk-git-index-"))
        backup = backup_dir / "index"
        had_index = index.exists()
        if had_index:
            shutil.copy2(index, backup)
        try:
            self._run(root, "add", "-A")
            self._run(root, "commit", "-m", clean_message)
        except Exception:
            if had_index:
                shutil.copy2(backup, index)
            elif index.exists():
                index.unlink()
            raise
        finally:
            shutil.rmtree(backup_dir, ignore_errors=True)
        return self.snapshot(workspace, include_patches=False)

    def require_repo(self, workspace: Path | str) -> Path:
        selected = self._workspace(workspace)
        root = self._repo_root(selected)
        if root is None:
            raise GitError("Ce dossier n’appartient à aucun dépôt Git.")
        return root

    @staticmethod
    def fallback_message(snapshot: GitSnapshot) -> str:
        paths = [item.path for item in snapshot.files]
        if len(paths) == 1:
            return f"Met à jour {paths[0]}"
        groups = sorted({Path(path).parts[0] for path in paths})
        scope = ", ".join(groups[:3])
        suffix = "…" if len(groups) > 3 else ""
        return f"Met à jour {len(paths)} fichiers dans {scope}{suffix}"

    def _repo_root(self, workspace: Path) -> Path | None:
        output = self._run(workspace, "rev-parse", "--show-toplevel", check=False).strip()
        return Path(output).resolve() if output else None

    @staticmethod
    def _workspace(workspace: Path | str) -> Path:
        selected = Path(workspace).expanduser().resolve()
        if not selected.is_dir():
            raise GitError(f"Dossier introuvable : {selected}")
        return selected

    def _status(self, root: Path) -> list[tuple[str, str]]:
        raw = self._run(
            root,
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        parts = raw.split("\0")
        result: list[tuple[str, str]] = []
        index = 0
        while index < len(parts):
            entry = parts[index]
            index += 1
            if not entry:
                continue
            xy, path = entry[:2], entry[3:]
            if "R" in xy or "C" in xy:
                if index < len(parts):
                    index += 1
            result.append((xy, path.replace("\\", "/")))
        return result

    def _patch(self, root: Path, relative: str, *, untracked: bool) -> str:
        if untracked:
            path = root / relative
            try:
                data = (
                    os.readlink(path).encode("utf-8", "surrogatepass")
                    if path.is_symlink()
                    else path.read_bytes()
                )
            except OSError:
                return ""
            if b"\0" in data[:8192]:
                return "Binary file\n"
            text = data.decode("utf-8", "replace")
            lines = text.splitlines()
            header = (
                f"diff --git a/{relative} b/{relative}\n"
                "new file mode 100644\n"
                f"--- /dev/null\n+++ b/{relative}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n"
            )
            return (header + "\n".join(f"+{line}" for line in lines))[: self.patch_limit]
        return self._run(
            root, "diff", "--no-ext-diff", "--no-color", "HEAD", "--", relative, check=False
        )

    def _file_fingerprint(self, root: Path, xy: str, relative: str) -> str:
        digest = hashlib.sha256()
        digest.update(xy.encode())
        digest.update(relative.encode("utf-8", "surrogatepass"))
        digest.update(self._run_bytes(root, "diff", "--cached", "--binary", "--", relative))
        path = root / relative
        try:
            if path.is_symlink():
                digest.update(os.readlink(path).encode("utf-8", "surrogatepass"))
            elif path.is_file():
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
        return digest.hexdigest()

    @staticmethod
    def _fingerprint(files: list[GitFile]) -> str:
        digest = hashlib.sha256()
        for item in files:
            digest.update(item.path.encode("utf-8", "surrogatepass"))
            digest.update(item.fingerprint.encode())
        return digest.hexdigest()

    @staticmethod
    def _counts(patch: str) -> tuple[int, int, bool]:
        binary = "Binary files" in patch or patch == "Binary file\n"
        additions = sum(
            1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        return additions, deletions, binary

    @staticmethod
    def _label(xy: str) -> str:
        if xy == "??":
            return "added"
        codes = set(xy.replace(" ", ""))
        if "D" in codes:
            return "deleted"
        if "R" in codes:
            return "renamed"
        if "A" in codes:
            return "added"
        return "modified"

    @staticmethod
    def _run(root: Path, *args: str, check: bool = True) -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=flags,
            )
        except FileNotFoundError as exc:
            raise GitError("Git n’est pas installé ou n’est pas accessible.") from exc
        if check and completed.returncode:
            raise GitError(completed.stderr.strip() or "La commande Git a échoué.")
        return completed.stdout

    @staticmethod
    def _run_bytes(root: Path, *args: str) -> bytes:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                check=False,
                creationflags=flags,
            )
        except FileNotFoundError as exc:
            raise GitError("Git n’est pas installé ou n’est pas accessible.") from exc
        return completed.stdout
