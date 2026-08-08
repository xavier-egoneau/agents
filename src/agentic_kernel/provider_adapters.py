from __future__ import annotations

import os
from typing import Protocol

import anthropic
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
from .models import ConnectionType, ProviderConfig


class CodexResponsesModel(OpenAIResponsesModel):
    """Consume Codex's mandatory stream behind the normal model contract."""

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


def model_settings(config: ProviderConfig) -> dict[str, object]:
    """Réglages déclarés dans `providers.json`, dans le vocabulaire du SDK.

    Cette fonction est partagée parce que son absence côté clé d'API a coûté un
    run complet : `num_predict` et `timeout_seconds` étaient acceptés dans la
    configuration, affichés dans l'interface, et jamais transmis au modèle. La
    limite appliquée restait celle du fournisseur, et l'erreur renvoyée invitait
    à « augmenter max_tokens » — un réglage qui existait déjà et ne servait à
    rien.

    Les clés absentes sont omises plutôt que mises à None : le SDK distingue
    « non précisé » de « nul ».
    """
    declared = {
        "temperature": config.temperature,
        "top_k": config.top_k,
        "top_p": config.top_p,
        "max_tokens": config.num_predict,
        "timeout": config.timeout_seconds,
    }
    return {key: value for key, value in declared.items() if value is not None}


class ProviderAdapter(Protocol):
    """Internal provider boundary; SDK-specific types never enter the public API."""

    key: str

    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object: ...


class LocalOpenAIAdapter:
    key = "local"

    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        base_url = local_base_url(config)
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(base_url=base_url, api_key="local"),
            settings=model_settings(config),
        )


class ApiKeyOpenAIAdapter:
    key = "api_key"

    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        env_name = config.api_key_env or default_key_env(config.kind)
        api_key = config.api_key or os.getenv(env_name)
        if not api_key:
            raise AuthenticationError(
                f"missing API key: set {env_name} or providers.json api_key"
            )
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(
                base_url=config.base_url or default_base_url(config.kind),
                api_key=api_key,
            ),
            settings=model_settings(config),
        )


class CodexOAuthAdapter:
    key = "openai-codex"

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
            # `openai_store` est imposé par ce fournisseur; le reste vient de la
            # configuration et ne doit pas l'écraser.
            settings={**model_settings(config), "openai_store": False},
        )


class ClaudeOAuthAdapter:
    key = "claude"

    def build(
        self,
        config: ProviderConfig,
        model_name: str,
        oauth: OAuthManager,
    ) -> object:
        credential = oauth.access_token("claude")
        client = anthropic.AsyncAnthropic(
            auth_token=credential.access,
            default_headers={
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20"
            },
        )
        return AnthropicModel(
            model_name,
            provider=AnthropicProvider(anthropic_client=client),
            settings=model_settings(config),
        )


class ProviderAdapterRegistry:
    """Explicit, replaceable registry for built-in and future provider adapters."""

    def __init__(self, adapters: list[ProviderAdapter] | None = None) -> None:
        selected = adapters or [
            LocalOpenAIAdapter(),
            ApiKeyOpenAIAdapter(),
            CodexOAuthAdapter(),
            ClaudeOAuthAdapter(),
        ]
        self._adapters: dict[str, ProviderAdapter] = {}
        for adapter in selected:
            self.register(adapter)

    def register(self, adapter: ProviderAdapter) -> None:
        if adapter.key in self._adapters:
            raise ConfigurationError(f"duplicate provider adapter: {adapter.key}")
        self._adapters[adapter.key] = adapter

    def for_config(self, config: ProviderConfig) -> ProviderAdapter:
        key = config.connection_type.value
        if config.connection_type == ConnectionType.AUTH:
            key = "openai-codex" if config.kind == "openai-codex" else "claude"
        adapter = self._adapters.get(key)
        if adapter is None:
            raise ConfigurationError(f"unsupported connection type for {config.id}")
        return adapter


def default_key_env(kind: str) -> str:
    return {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(kind, f"{kind.upper().replace('-', '_')}_API_KEY")


def default_base_url(kind: str) -> str | None:
    return {
        "deepseek": "https://api.deepseek.com",
        "qwen": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    }.get(kind)


def local_base_url(config: ProviderConfig) -> str:
    if config.base_url:
        return config.base_url.rstrip("/")
    port = getattr(config, "port", None)
    if isinstance(port, int) and 1 <= port <= 65535:
        return f"http://127.0.0.1:{port}"
    raise ConfigurationError(f"local provider {config.id} requires base_url or port")
