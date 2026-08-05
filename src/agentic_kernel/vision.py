from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .errors import KernelError
from .managed_tools import discovered_executable
from .module_settings import stored_module_settings


class VisionUnavailable(KernelError):
    pass


class LocalVisionService:
    """Gemma vision through a loopback OpenAI-compatible llama.cpp server."""

    def __init__(self, content_root: Path) -> None:
        self.content_root = content_root
        self.config_path = content_root / "vision.json"
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    async def prepare(self) -> None:
        """Install/download and load the configured model once."""
        await self._ensure_server(self._config())

    def installed_assets(self) -> tuple[str | None, str | None]:
        """Return configured local vision assets that are already present."""
        config = self._config()
        binary_name = str(config.get("server_binary") or "llama-server")
        binary_path = Path(binary_name).expanduser()
        binary = (
            str(binary_path.resolve())
            if binary_path.is_file()
            else discovered_executable("llama", "AMK_LLAMA_BIN")
        )
        model = None
        model_value = config.get("model_path")
        if isinstance(model_value, str) and model_value.strip():
            model_path = Path(model_value).expanduser()
            if model_path.is_file():
                model = str(model_path.resolve())
        return binary, model

    def close(self) -> None:
        """Stop only the llama-server process started by this service."""
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        (self.content_root / "vision" / "llama-server.pid").unlink(missing_ok=True)

    async def analyze_path(self, path: Path, question: str, detail: str = "balanced") -> str:
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise VisionUnavailable(f"image locale illisible : {path}") from exc
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(path.suffix.lower(), "image/png")
        return await self.analyze_bytes(data, media_type, question, detail)

    async def analyze_bytes(
        self,
        data: bytes,
        media_type: str,
        question: str,
        detail: str = "balanced",
    ) -> str:
        config = self._config()
        await self._ensure_server(config)
        budgets = {"fast": 70, "balanced": 280, "precise": 1120}
        visual_budget = budgets.get(detail, 280)
        prompt = (
            f"{question.strip() or 'Décris précisément cette image.'}\n\n"
            f"Budget visuel souhaité : {visual_budget} tokens. Réponds en texte "
            "factuel et borné. Pour une interface, relève aussi le texte visible, "
            "la structure, les états et les anomalies. Distingue observations et "
            "incertitudes."
        )
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": config["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": int(config.get("max_tokens", 2048)),
        }
        try:
            async with httpx.AsyncClient(
                timeout=float(config.get("timeout_seconds", 180))
            ) as client:
                response = await client.post(
                    config["base_url"].rstrip("/") + "/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VisionUnavailable(
                "Gemma 4 local est indisponible. Vérifie `content-agents/vision.json` "
                "et le serveur llama.cpp."
            ) from exc
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionUnavailable("réponse Gemma 4 locale invalide") from exc
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise VisionUnavailable("Gemma 4 local n’a retourné aucune observation")
        return content.strip()[:20_000]

    def _config(self) -> dict[str, object]:
        defaults: dict[str, object] = {
            "base_url": "http://127.0.0.1:8081/v1",
            "model": "gemma-4-E2B-it",
            "model_path": None,
            "hf_repo": "ggml-org/gemma-4-E2B-it-GGUF:Q4_0",
            "mmproj_path": None,
            "server_binary": "llama-server",
            "port": 8081,
            "timeout_seconds": 180,
            "startup_timeout_seconds": 1800,
            "max_tokens": 2048,
            "llama_args": [],
        }
        if self.config_path.is_file():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise VisionUnavailable(
                    f"configuration vision invalide : {self.config_path}"
                ) from exc
            if not isinstance(raw, dict):
                raise VisionUnavailable("la configuration vision doit être un objet")
            defaults.update(raw)
        try:
            defaults.update(stored_module_settings(self.content_root, "perception"))
        except KernelError as exc:
            raise VisionUnavailable(str(exc)) from exc
        parsed = urlparse(str(defaults["base_url"]))
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise VisionUnavailable(
                "le moteur vision doit utiliser une adresse HTTP loopback locale"
            )
        return defaults

    async def _ensure_server(self, config: dict[str, object]) -> None:
        if await self._reachable(str(config["base_url"])):
            return
        if await self._wait_for_existing_server(config):
            return
        async with self._lock:
            if await self._reachable(str(config["base_url"])):
                return
            if await self._wait_for_existing_server(config):
                return
            binary_name = str(config.get("server_binary") or "llama-server")
            binary = (
                binary_name
                if Path(binary_name).is_file()
                else discovered_executable("llama", "AMK_LLAMA_BIN")
            )
            if not binary:
                raise VisionUnavailable(
                    "`llama-server` est absent. Installe llama.cpp ou configure "
                    "`server_binary` dans `content-agents/vision.json`."
                )
            command = [
                binary,
                "--host",
                "127.0.0.1",
                "--port",
                str(config.get("port", 8081)),
            ]
            model_path = config.get("model_path")
            hf_repo = config.get("hf_repo")
            if isinstance(model_path, str) and model_path.strip():
                model = Path(model_path).expanduser().resolve()
                if not model.is_file():
                    raise VisionUnavailable(f"modèle Gemma introuvable : {model}")
                command.extend(["-m", str(model)])
            elif isinstance(hf_repo, str) and hf_repo.strip():
                command.extend(["-hf", hf_repo.strip()])
            else:
                raise VisionUnavailable(
                    "Configure `model_path` ou `hf_repo` dans `content-agents/vision.json`."
                )
            mmproj = config.get("mmproj_path")
            if isinstance(mmproj, str) and mmproj.strip():
                projection = Path(mmproj).expanduser().resolve()
                if not projection.is_file():
                    raise VisionUnavailable(f"projection vision introuvable : {projection}")
                command.extend(["--mmproj", str(projection)])
            extra = config.get("llama_args", [])
            if isinstance(extra, list) and all(isinstance(item, str) for item in extra):
                command.extend(extra)
            logs = self.content_root / "vision"
            logs.mkdir(parents=True, exist_ok=True)
            stream = (logs / "llama-server.log").open("ab", buffering=0)
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "LLAMA_ARG_NO_WEBUI": "1"},
            )
            stream.close()
            (logs / "llama-server.pid").write_text(str(self._process.pid), encoding="utf-8")
            deadline = asyncio.get_running_loop().time() + float(
                config.get("startup_timeout_seconds", 120)
            )
            while asyncio.get_running_loop().time() < deadline:
                if self._process.poll() is not None:
                    raise VisionUnavailable(
                        "llama-server s’est arrêté pendant le chargement de Gemma 4"
                    )
                if await self._reachable(str(config["base_url"])):
                    return
                await asyncio.sleep(0.5)
            raise VisionUnavailable("délai de démarrage de Gemma 4 dépassé")

    async def _wait_for_existing_server(self, config: dict[str, object]) -> bool:
        pid_path = self.content_root / "vision" / "llama-server.pid"
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            pid_path.unlink(missing_ok=True)
            return False
        deadline = asyncio.get_running_loop().time() + float(
            config.get("startup_timeout_seconds", 1800)
        )
        while asyncio.get_running_loop().time() < deadline:
            if await self._reachable(str(config["base_url"])):
                return True
            try:
                os.kill(pid, 0)
            except OSError:
                pid_path.unlink(missing_ok=True)
                return False
            await asyncio.sleep(0.5)
        raise VisionUnavailable("délai de démarrage de Gemma 4 dépassé")

    @staticmethod
    async def _reachable(base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(base_url.rstrip("/").removesuffix("/v1") + "/health")
            return response.status_code < 500
        except httpx.HTTPError:
            return False
