"""Install and supervise the loopback-only SearXNG service used by Ketch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

import httpx
import yaml

from .errors import ConfigurationError
from .installation import data_home
from .managed_tools import discovered_executable
from .platform.processes import (
    listening_pids,
    process_command,
    stop_async_process,
    subprocess_group_kwargs,
)

SEARXNG_REVISION = "c63835bd2a5133b30b3752a20eac6b443a918f41"
SEARXNG_ARCHIVE_SHA256 = "219fc11590c59c1e0d314f17f06e26bc55592e2f519950da9741699d060d3577"
SEARXNG_ARCHIVE_URL = (
    f"https://github.com/searxng/searxng/archive/{SEARXNG_REVISION}.tar.gz"
)
SOURCE_DIRECTORIES = ("searx/", "searxng_extra/")
SOURCE_FILES = {"babel.cfg", "requirements.txt", "requirements-server.txt", "setup.py"}


class SearxngService:
    """Own one SearXNG process and its isolated, versioned Python environment."""

    def __init__(self, root: Path | None = None, port: int | None = None) -> None:
        self.root = (root or data_home() / "services" / "searxng").expanduser().resolve()
        self.port = port or int(os.environ.get("AMK_SEARXNG_PORT", "8888"))
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.source = self.root / "source"
        self.venv = self.root / "venv"
        self.settings = self.root / "settings.yml"
        self.manifest = self.root / "manifest.json"
        self.logs = self.root / "logs"
        self._process: asyncio.subprocess.Process | None = None

    @property
    def python(self) -> Path:
        suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        return self.venv / suffix

    def prepare(self) -> bool:
        """Install the pinned source and configure Ketch. Return whether files changed."""
        if self._ready():
            self._configure_ketch()
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="amk-searxng-") as raw_temp:
            temporary = Path(raw_temp)
            archive = temporary / "source.tar.gz"
            self._download(archive)
            staged_source = temporary / "source"
            self._extract(archive, staged_source)
            staged_venv = temporary / "venv"
            self._install_requirements(staged_source, staged_venv)
            if self.source.exists():
                shutil.rmtree(self.source)
            if self.venv.exists():
                shutil.rmtree(self.venv)
            shutil.move(str(staged_source), self.source)
            shutil.move(str(staged_venv), self.venv)
        self._write_settings()
        self.manifest.write_text(
            json.dumps({"revision": SEARXNG_REVISION, "port": self.port}, indent=2) + "\n",
            encoding="utf-8",
        )
        self._configure_ketch()
        return True

    def installed(self) -> bool:
        """Read-only check for diagnostics: no reconfiguration, no side effects."""
        try:
            state = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return state.get("revision") == SEARXNG_REVISION and self.python.is_file()

    def _ready(self) -> bool:
        try:
            state = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if state.get("revision") != SEARXNG_REVISION or not self.python.is_file():
            return False
        if state.get("port") != self.port or not self.settings.is_file():
            self._write_settings()
            state["port"] = self.port
            self.manifest.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return True

    @staticmethod
    def _download(destination: Path) -> None:
        request = urllib.request.Request(
            SEARXNG_ARCHIVE_URL, headers={"User-Agent": "AMK-SearXNG-installer"}
        )
        with (
            urllib.request.urlopen(request, timeout=120) as response,  # noqa: S310
            destination.open("wb") as out,
        ):
            shutil.copyfileobj(response, out)
        actual = hashlib.sha256(destination.read_bytes()).hexdigest()
        if actual != SEARXNG_ARCHIVE_SHA256:
            raise ConfigurationError("SearXNG archive checksum mismatch")

    @staticmethod
    def _extract(archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True)
        with tarfile.open(archive, "r:gz") as package:
            for member in package:
                parts = PurePosixPath(member.name).parts
                if len(parts) < 2 or not member.isfile():
                    continue
                relative = PurePosixPath(*parts[1:])
                name = relative.as_posix()
                if name not in SOURCE_FILES and not name.startswith(SOURCE_DIRECTORIES):
                    continue
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = package.extractfile(member)
                if stream is None:
                    continue
                with stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
        if os.name == "nt":
            (destination / "pwd.py").write_text(
                '"""Compatibility for SearXNG optional Valkey diagnostics on Windows."""\n'
                "from types import SimpleNamespace\n\n"
                "def getpwuid(uid: int):\n"
                '    return SimpleNamespace(pw_name="amk", pw_uid=uid)\n',
                encoding="utf-8",
            )

    @staticmethod
    def _install_requirements(source: Path, venv: Path) -> None:
        uv = shutil.which("uv")
        if uv:
            subprocess.run([uv, "venv", "--python", sys.executable, str(venv)], check=True)
            subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
                    "-r",
                    str(source / "requirements.txt"),
                    "-r",
                    str(source / "requirements-server.txt"),
                ],
                check=True,
            )
            return
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-r",
                str(source / "requirements.txt"),
                "-r",
                str(source / "requirements-server.txt"),
            ],
            check=True,
        )

    def _write_settings(self) -> None:
        secret_file = self.root / ".secret"
        try:
            secret = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            secret = secrets.token_hex(32)
            secret_file.write_text(secret + "\n", encoding="utf-8")
        document = {
            "use_default_settings": True,
            "general": {"debug": False, "instance_name": "AMK Search"},
            "search": {"safe_search": 1, "formats": ["html", "json"]},
            "server": {
                "bind_address": "127.0.0.1",
                "port": self.port,
                "secret_key": secret,
                "limiter": False,
                "image_proxy": False,
            },
        }
        self.settings.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    def _configure_ketch(self) -> None:
        ketch = discovered_executable("ketch", "AMK_KETCH_BIN")
        if ketch is None:
            return
        for key, value in (("searxng_url", self.base_url), ("backend", "searxng")):
            result = subprocess.run(
                [ketch, "config", "set", key, value], capture_output=True, text=True
            )
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                raise ConfigurationError(f"Ketch configuration failed: {detail}")

    async def start(self, timeout: float = 45) -> None:
        if await self._reachable():
            return
        occupants = listening_pids(self.port)
        if occupants:
            descriptions = ", ".join(
                f"PID {pid} ({process_command(pid) or 'commande inconnue'})" for pid in occupants
            )
            raise ConfigurationError(f"SearXNG port {self.port} is already used by {descriptions}")
        if not self._ready():
            raise ConfigurationError(
                "SearXNG is not installed; run AMK through `amk serve` or `amk web`"
            )
        self.logs.mkdir(parents=True, exist_ok=True)
        log = (self.logs / "server.log").open("ab")
        environment = {**os.environ, "SEARXNG_SETTINGS_PATH": str(self.settings)}
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(self.python),
                "-m",
                "searx.webapp",
                cwd=self.source,
                env=environment,
                stdout=log,
                stderr=log,
                **subprocess_group_kwargs(),
            )
        finally:
            log.close()
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                raise ConfigurationError(
                    f"SearXNG stopped during startup; see {self.logs / 'server.log'}"
                )
            if await self._reachable():
                return
            await asyncio.sleep(0.25)
        await self.stop()
        raise ConfigurationError(f"SearXNG startup timed out; see {self.logs / 'server.log'}")

    async def stop(self) -> None:
        if self._process is not None:
            await stop_async_process(self._process)
            self._process = None

    async def _reachable(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(
                    f"{self.base_url}/search", params={"q": "amk", "format": "json"}
                )
            content_type = response.headers.get("content-type", "")
            return response.status_code == 200 and content_type.startswith("application/json")
        except httpx.HTTPError:
            return False
