from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import httpx

from .auth import OAuthManager
from .errors import AuthenticationError, ConfigurationError
from .models import ConnectionType, ProviderConfig, ProviderRegistry
from .provider_adapters import (
    ProviderAdapterRegistry,
    default_key_env,
    local_base_url,
)


class ProviderFactory:
    def __init__(
        self,
        registry: ProviderRegistry,
        oauth: OAuthManager | None = None,
        adapters: ProviderAdapterRegistry | None = None,
    ) -> None:
        self.registry = registry
        self.oauth = oauth or OAuthManager()
        self.adapters = adapters or ProviderAdapterRegistry()

    def get_config(self, provider_id: str) -> ProviderConfig:
        for provider in self.registry.providers:
            if provider.id == provider_id:
                return provider
        raise ConfigurationError(f"unknown provider: {provider_id}")

    def build(self, provider_id: str, model_override: str | None = None):
        config = self.get_config(provider_id)
        model_name = model_override or config.model
        if not model_name:
            raise ConfigurationError(
                f"provider {provider_id} requires a model selected for this run"
            )
        adapter = self.adapters.for_config(config)
        return adapter.build(config, model_name, self.oauth)

    async def check(self, provider_id: str) -> tuple[bool, str]:
        config = self.get_config(provider_id)
        if config.connection_type == ConnectionType.LOCAL:
            try:
                base_url = local_base_url(config)
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(base_url.rstrip("/") + "/v1/models")
                response.raise_for_status()
                return True, "reachable"
            except httpx.HTTPError as exc:
                return False, str(exc)
        try:
            self.build(provider_id)
            return True, "configured"
        except (AuthenticationError, ConfigurationError) as exc:
            return False, str(exc)

    async def list_models(self, provider_id: str) -> tuple[list[str], str, str | None]:
        """Discover provider models live, with a deterministic configured fallback."""
        config = self.get_config(provider_id)
        configured = [
            model
            for model in dict.fromkeys([config.model, *config.models])
            if isinstance(model, str) and model
        ]

        models_dir = getattr(config, "models_dir", None)
        if config.connection_type == ConnectionType.LOCAL and models_dir:
            directory = Path(str(models_dir)).expanduser()
            if directory.is_dir():
                discovered = sorted(
                    {
                        path.name[:-5]
                        for path in directory.iterdir()
                        if path.is_file() and path.name.lower().endswith(".gguf")
                    }
                )
                if discovered:
                    return discovered, "directory", None

        try:
            headers: dict[str, str] = {}
            base_url = config.base_url
            if config.connection_type == ConnectionType.LOCAL:
                base_url = local_base_url(config)
            elif config.connection_type == ConnectionType.API_KEY:
                env_name = config.api_key_env or default_key_env(config.kind)
                api_key = config.api_key or os.getenv(env_name)
                if not api_key:
                    raise AuthenticationError(f"missing API key: {env_name}")
                headers["authorization"] = f"Bearer {api_key}"
            elif config.connection_type == ConnectionType.AUTH:
                auth_id = "openai-codex" if config.kind == "openai-codex" else "claude"
                credential = self.oauth.access_token(auth_id)
                if auth_id == "openai-codex":
                    headers.update(
                        {
                            "authorization": f"Bearer {credential.access}",
                            "chatgpt-account-id": credential["account_id"],
                        }
                    )
                else:
                    headers.update(
                        {
                            "authorization": f"Bearer {credential.access}",
                            "anthropic-version": "2023-06-01",
                            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                        }
                    )
            if not base_url:
                raise ConfigurationError("provider has no model-list endpoint")
            candidates: list[tuple[str, dict[str, str] | None]] = [
                (base_url.rstrip("/") + "/models", None),
                (base_url.rstrip("/") + "/v1/models", None),
            ]
            if base_url.rstrip("/").endswith("/v1"):
                candidates = [(base_url.rstrip("/") + "/models", None)]
            elif config.kind == "openai-codex":
                # ChatGPT's Codex catalog is not OpenAI-compatible: it requires
                # the client version and returns model identifiers as `slug`.
                candidates = [
                    (
                        base_url.rstrip("/") + "/models",
                        {"client_version": _codex_client_version()},
                    )
                ]
            last_error: Exception | None = None
            async with httpx.AsyncClient(timeout=min(config.timeout_seconds, 5)) as client:
                seen_urls: set[str] = set()
                for url, params in candidates:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    try:
                        response = await client.get(url, headers=headers, params=params)
                        response.raise_for_status()
                        payload = response.json()
                        items = payload.get("data", payload.get("models", []))
                        names = [
                            item.get("id") or item.get("name") or item.get("slug")
                            for item in items
                            if isinstance(item, dict) and item.get("visibility") != "hide"
                        ]
                        live = sorted({name for name in names if isinstance(name, str)})
                        if live:
                            return live, "live", None
                    except (httpx.HTTPError, ValueError) as exc:
                        last_error = exc
            if last_error:
                raise last_error
        except Exception as exc:
            return configured, "configured", str(exc)[:240]
        return configured, "configured", None


def _codex_client_version() -> str:
    """Report the local Codex version used to filter the remote model catalog."""
    override = os.getenv("AMK_CODEX_CLIENT_VERSION")
    if override:
        return override
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
        match = re.search(r"\d+\.\d+\.\d+(?:-[\w.]+)?", result.stdout)
        if match:
            return match.group(0)
    except (OSError, subprocess.SubprocessError):
        pass
    # Compatible baseline for the current Codex model-catalog contract.
    return "0.145.0"
