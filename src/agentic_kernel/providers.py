from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

import anthropic
import httpx
from openai import AsyncOpenAI
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from .auth import OAuthManager
from .errors import AuthenticationError, ConfigurationError
from .models import ConnectionType, ProviderConfig, ProviderRegistry


class CodexResponsesModel(OpenAIResponsesModel):
    """Responses adapter for ChatGPT's Codex endpoint.

    That endpoint only accepts streamed requests. Kernel callers may still use
    ``Agent.run``: the adapter consumes the provider stream and returns the same
    complete Pydantic AI response contract.
    """

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self.request_stream(
            messages, model_settings, model_request_parameters
        ) as streamed:
            async for _ in streamed:
                pass
        return streamed.get()


class ProviderAdapter(Protocol):
    """Internal provider boundary; no SDK-specific type crosses the public API."""

    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object: ...


class LocalOpenAIAdapter:
    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        base_url = _local_base_url(config)
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(base_url=base_url, api_key="local"),
        )


class ApiKeyOpenAIAdapter:
    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        env_name = config.api_key_env or _default_key_env(config.kind)
        api_key = config.api_key or os.getenv(env_name)
        if not api_key:
            raise AuthenticationError(f"missing API key: set {env_name} or providers.json api_key")
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(
                base_url=config.base_url or _default_base_url(config.kind),
                api_key=api_key,
            ),
        )


class CodexOAuthAdapter:
    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        credential = oauth.access_token("openai-codex")
        client = AsyncOpenAI(
            api_key=credential.access,
            base_url="https://chatgpt.com/backend-api/codex",
            default_headers={"chatgpt-account-id": credential["account_id"]},
        )
        return CodexResponsesModel(
            model_name,
            provider=OpenAIProvider(openai_client=client),
            settings={"openai_store": False},
        )


class ClaudeOAuthAdapter:
    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        credential = oauth.access_token("claude")
        client = anthropic.AsyncAnthropic(
            auth_token=credential.access,
            default_headers={"anthropic-beta": "claude-code-20250219,oauth-2025-04-20"},
        )
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(anthropic_client=client),
        )


class ProviderFactory:
    def __init__(self, registry: ProviderRegistry, oauth: OAuthManager | None = None) -> None:
        self.registry = registry
        self.oauth = oauth or OAuthManager()
        self.adapters: dict[str, ProviderAdapter] = {
            "local": LocalOpenAIAdapter(),
            "api_key": ApiKeyOpenAIAdapter(),
            "openai-codex": CodexOAuthAdapter(),
            "claude": ClaudeOAuthAdapter(),
        }

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
        adapter_key = config.connection_type.value
        if config.connection_type == ConnectionType.AUTH:
            adapter_key = "openai-codex" if config.kind == "openai-codex" else "claude"
        adapter = self.adapters.get(adapter_key)
        if adapter is None:
            raise ConfigurationError(f"unsupported connection type for {provider_id}")
        return adapter.build(config, model_name, self.oauth)

    async def check(self, provider_id: str) -> tuple[bool, str]:
        config = self.get_config(provider_id)
        if config.connection_type == ConnectionType.LOCAL:
            try:
                base_url = _local_base_url(config)
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
                base_url = _local_base_url(config)
            elif config.connection_type == ConnectionType.API_KEY:
                env_name = config.api_key_env or _default_key_env(config.kind)
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


def _default_key_env(kind: str) -> str:
    return {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(kind, f"{kind.upper().replace('-', '_')}_API_KEY")


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


def _default_base_url(kind: str) -> str | None:
    return {"deepseek": "https://api.deepseek.com"}.get(kind)


def _local_base_url(config: ProviderConfig) -> str:
    if config.base_url:
        return config.base_url.rstrip("/")
    port = getattr(config, "port", None)
    if isinstance(port, int) and 1 <= port <= 65535:
        return f"http://127.0.0.1:{port}"
    raise ConfigurationError(f"local provider {config.id} requires base_url or port")
