from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import httpx

from .auth import OAuthManager
from .errors import AuthenticationError, ConfigurationError
from .llama_server import (
    LlamaServerError,
    LlamaServerManager,
    ThroughputCounters,
    scan_gguf_models,
)
from .models import ConnectionType, ProviderConfig, ProviderRegistry
from .provider_adapters import (
    ProviderAdapterRegistry,
    default_key_env,
    local_base_url,
)


def compaction_trigger_ratio(config: ProviderConfig) -> float:
    """Seuil de déclenchement de la compaction, selon la nature du provider.

    Un modèle local sert à volonté et sa fenêtre est connue précisément
    (allouée par le serveur) : on peut la remplir à 90 % avant de compacter.
    Un provider cloud facture chaque token et applique ses propres plafonds :
    on conserve une marge de sécurité plus large.
    """
    return 0.9 if config.connection_type == ConnectionType.LOCAL else 0.7


def server_context_cap(config: ProviderConfig) -> int | None:
    """Fenêtre réellement allouée au démarrage d'un serveur llama.cpp administré.

    `--ctx-size` est un plafond dur côté inférence : une déclaration plus
    grande via /model-context ne peut pas être servie.
    """
    if config.kind not in {"llama-cpp", "llama.cpp"} or not config.models_dir:
        return None
    return config.num_ctx


class ProviderFactory:
    def __init__(
        self,
        registry: ProviderRegistry,
        oauth: OAuthManager | None = None,
        adapters: ProviderAdapterRegistry | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.registry = registry
        self.oauth = oauth or OAuthManager()
        self.adapters = adapters or ProviderAdapterRegistry()
        self.runtime_dir = runtime_dir

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
        manager = self._managed_llama(config)
        if manager is not None:
            try:
                manager.ensure_running(model_name)
            except LlamaServerError as exc:
                raise ConfigurationError(str(exc)) from exc
        adapter = self.adapters.for_config(config)
        return adapter.build(config, model_name, self.oauth)

    def throughput_counters(self, provider_id: str) -> ThroughputCounters | None:
        """Compteurs de débit du serveur local qui sert ce provider, s'il y en a un.

        Renvoie `None` pour tout provider distant : DeepSeek et consorts ne
        publient pas la décomposition entre traitement du prompt et écriture.
        """
        try:
            manager = self._managed_llama(self.get_config(provider_id))
        except ConfigurationError:
            return None
        return manager.throughput_counters() if manager is not None else None

    async def check(self, provider_id: str) -> tuple[bool, str]:
        config = self.get_config(provider_id)
        if config.connection_type == ConnectionType.LOCAL:
            manager = self._managed_llama(config)
            if manager is not None:
                try:
                    manager.validate_configuration()
                    state = manager.status()
                    if state is None:
                        return True, "configured; starts on first use"
                    if manager.health_ok():
                        return True, f"reachable; model={state.model}; pid={state.pid}"
                    return False, f"llama-server pid {state.pid} is not healthy"
                except LlamaServerError as exc:
                    return False, str(exc)
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

    async def list_models(  # noqa: C901 - dette: catalogage par provider
        self, provider_id: str
    ) -> tuple[list[str], str, str | None]:
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
                discovered = scan_gguf_models(directory)
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

    def managed_llama(self, provider_id: str) -> LlamaServerManager | None:
        """Expose lifecycle controls without starting a configured server."""
        return self._managed_llama(self.get_config(provider_id))

    def _managed_llama(self, config: ProviderConfig) -> LlamaServerManager | None:
        if config.kind not in {"llama-cpp", "llama.cpp"} or not config.models_dir:
            return None
        if self.runtime_dir is None:
            raise ConfigurationError(
                "managed llama.cpp requires a provider runtime directory"
            )
        server_args: list[str] = []
        option_pairs = (
            (config.n_gpu_layers, "--n-gpu-layers"),
            (config.num_ctx, "--ctx-size"),
            (config.threads, "--threads"),
            (config.parallel, "--parallel"),
            (config.batch_size, "--batch-size"),
            (config.ubatch_size, "--ubatch-size"),
        )
        for value, option in option_pairs:
            if value is not None:
                server_args.extend([option, str(value)])
        if config.flash_attn is True:
            server_args.extend(["--flash-attn", "on"])
        elif config.flash_attn is False:
            server_args.extend(["--flash-attn", "off"])
        # Sans cet indicateur, `/metrics` répond 501 et la vitesse réelle
        # d'écriture reste invisible : l'interface ne peut alors qu'afficher des
        # tokens divisés par une durée, ce qui n'est pas une vitesse.
        server_args.append("--metrics")
        server_args.extend(config.llama_args)
        return LlamaServerManager(
            state_dir=self.runtime_dir,
            models_dir=Path(config.models_dir),
            provider_id=config.id,
            binary=config.server_binary,
            port=config.port or 8123,
            server_args=server_args,
            startup_timeout_seconds=config.startup_timeout_seconds or 180,
        )


def _codex_client_version() -> str:
    """Report the local Codex version used to filter the remote model catalog."""
    override = os.getenv("AMK_CODEX_CLIENT_VERSION")
    if override:
        return override
    try:
        result = subprocess.run(
            ["codex", "--version"],  # noqa: S607 - codex résolu via PATH
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
