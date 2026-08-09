"""Install small, platform-specific AMK companion binaries from GitHub releases."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .installation import data_home

GITHUB_API = "https://api.github.com/repos/{repository}/releases/latest"

# CodeGraph n'a pas de releases GitHub par plateforme : c'est un paquet npm
# global, installe et decouvert separement du pipeline ToolSpec ci-dessous.
CODEGRAPH_NPM_PACKAGE = "@colbymchenry/codegraph"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    repository: str
    executable: str
    asset_fragment: str


def _platform_values() -> tuple[str, str]:
    systems = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}
    machines = {
        "x86_64": "x86_64",
        "AMD64": "x86_64",
        "aarch64": "arm64",
        "ARM64": "arm64",
        "arm64": "arm64",
    }
    try:
        return systems[platform.system()], machines[platform.machine()]
    except KeyError as exc:
        raise ConfigurationError(
            f"unsupported managed-tool platform: {platform.system()} {platform.machine()}"
        ) from exc


def tool_specs() -> dict[str, ToolSpec]:
    system, architecture = _platform_values()
    suffix = ".exe" if system == "windows" else ""
    llama_system = {"windows": "win-cpu", "darwin": "macos", "linux": "ubuntu"}[system]
    llama_arch = "x64" if architecture == "x86_64" else architecture
    return {
        "ketch": ToolSpec(
            name="ketch",
            repository="1broseidon/ketch",
            executable=f"ketch{suffix}",
            asset_fragment=f"_{system}_{architecture}",
        ),
        "llama": ToolSpec(
            name="llama",
            repository="ggml-org/llama.cpp",
            executable=f"llama-server{suffix}",
            asset_fragment=f"-bin-{llama_system}-{llama_arch}",
        ),
    }


def tools_home() -> Path:
    return data_home().expanduser().resolve() / "tools"


def managed_executable(name: str) -> str | None:
    spec = tool_specs().get(name)
    if spec is None:
        return None
    manifest = tools_home() / name / "current.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        path = Path(document["executable"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return str(path) if path.is_file() else None


def discovered_executable(name: str, env_var: str | None = None) -> str | None:
    """Find an existing tool without requiring it to be managed by AMK."""
    spec = tool_specs().get(name)
    if spec is None:
        return None
    if env_var and (configured := os.getenv(env_var)):
        path = Path(configured).expanduser()
        return str(path.resolve()) if path.is_file() else None
    if on_path := shutil.which(spec.executable):
        return str(Path(on_path).resolve())
    for directory in (Path.home() / "bin", Path.home() / ".local" / "bin"):
        candidate = directory / spec.executable
        if candidate.is_file():
            return str(candidate.resolve())
    return managed_executable(name)


def discovered_codegraph_executable() -> str | None:
    """Find an existing CodeGraph CLI, managed by npm rather than AMK."""
    if configured := os.getenv("AMK_CODEGRAPH_BIN"):
        candidate = Path(configured).expanduser()
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    if on_path := shutil.which("codegraph"):
        return on_path
    user_local = Path.home() / ".local" / "bin" / "codegraph"
    if user_local.is_file() and os.access(user_local, os.X_OK):
        return str(user_local)
    return None


def install_codegraph(npm: str) -> str:
    """Install the CodeGraph CLI globally via npm and return its resolved path."""
    subprocess.run([npm, "install", "--global", CODEGRAPH_NPM_PACKAGE], check=True)  # noqa: S603 - npm absolu, paquet constant
    executable = discovered_codegraph_executable()
    if executable is None:
        raise ConfigurationError(
            f"{CODEGRAPH_NPM_PACKAGE} installe, mais l'executable codegraph reste introuvable "
            "sur le PATH"
        )
    return executable


class ManagedToolInstaller:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or tools_home()).expanduser().resolve()

    def install(self, name: str) -> Path:
        spec = tool_specs().get(name)
        if spec is None:
            raise ConfigurationError(f"unknown managed tool: {name}")
        release = self._release(spec.repository)
        asset = self._asset(release, spec.asset_fragment)
        tag = str(release.get("tag_name") or "latest")
        target = self.root / name / tag
        existing = self._find_executable(target, spec.executable)
        if existing is not None:
            self._record(name, tag, existing, asset)
            return existing

        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"amk-{name}-") as raw_temp:
            temporary = Path(raw_temp)
            archive = temporary / str(asset["name"])
            self._download(str(asset["browser_download_url"]), archive)
            self._verify(archive, str(asset.get("digest") or ""))
            extracted = temporary / "extracted"
            extracted.mkdir()
            self._extract(archive, extracted)
            executable = self._find_executable(extracted, spec.executable)
            if executable is None:
                raise ConfigurationError(f"{spec.executable} is absent from {asset['name']}")
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(extracted), str(target))
        installed = self._find_executable(target, spec.executable)
        if installed is None:
            raise ConfigurationError(f"failed to install {name}")
        if os.name != "nt":
            installed.chmod(installed.stat().st_mode | 0o700)
        self._record(name, tag, installed, asset)
        return installed

    @staticmethod
    def _release(repository: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - URL GitHub API constante (https)
            GITHUB_API.format(repository=repository),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "AMK-installer"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)

    @staticmethod
    def _asset(release: dict[str, Any], fragment: str) -> dict[str, Any]:
        matches = [
            asset
            for asset in release.get("assets", [])
            if isinstance(asset, dict)
            and fragment in str(asset.get("name", ""))
            and str(asset.get("name", "")).endswith((".zip", ".tar.gz"))
        ]
        if len(matches) != 1:
            raise ConfigurationError(f"release asset not found or ambiguous for {fragment}")
        return matches[0]

    @staticmethod
    def _download(url: str, destination: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "AMK-installer"})  # noqa: S310 - URL d'asset de release GitHub (https)
        with (
            urllib.request.urlopen(request, timeout=120) as response,  # noqa: S310
            destination.open("wb") as out,
        ):
            shutil.copyfileobj(response, out)

    @staticmethod
    def _verify(path: Path, digest: str) -> None:
        if not digest.startswith("sha256:"):
            raise ConfigurationError(f"release asset has no SHA-256 digest: {path.name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest.removeprefix("sha256:").lower():
            raise ConfigurationError(f"checksum mismatch for {path.name}")

    @staticmethod
    def _extract(archive: Path, destination: Path) -> None:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    target = (destination / member.filename).resolve()
                    target.relative_to(destination.resolve())
                package.extractall(destination)  # noqa: S202 - chemins contrôlés ci-dessus
            return
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(destination, filter="data")

    @staticmethod
    def _find_executable(root: Path, filename: str) -> Path | None:
        if not root.is_dir():
            return None
        return next((path.resolve() for path in root.rglob(filename) if path.is_file()), None)

    def _record(self, name: str, tag: str, executable: Path, asset: dict[str, Any]) -> None:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "current.json.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "version": tag,
                    "executable": str(executable.resolve()),
                    "asset": asset.get("name"),
                    "digest": asset.get("digest"),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, directory / "current.json")
