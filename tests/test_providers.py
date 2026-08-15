import httpx
import pytest
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from agentic_kernel.errors import AuthenticationError, ConfigurationError
from agentic_kernel.models import ProviderRegistry
from agentic_kernel.provider_adapters import ProviderAdapterRegistry
from agentic_kernel.providers import ProviderFactory


def registry(connection_type: str, **extra):
    provider = {
        "id": "provider",
        "kind": "deepseek" if connection_type == "api_key" else "llama-cpp",
        "connection_type": connection_type,
        "model": "model",
        **extra,
    }
    return ProviderRegistry(
        default_provider="provider",
        providers=[provider],  # type: ignore[list-item]
    )


def test_llama_cpp_legacy_port_builds_local_model() -> None:
    model = ProviderFactory(registry("local", port=8123)).build("provider")
    assert isinstance(model, OpenAIChatModel)
    assert str(model.base_url) == "http://127.0.0.1:8123/v1/"


def test_local_sampling_settings_are_applied() -> None:
    model = ProviderFactory(
        registry("local", port=8123, temperature=0, top_k=1, num_predict=2048)
    ).build("provider")
    assert model.settings["temperature"] == 0
    assert model.settings["top_k"] == 1
    assert model.settings["max_tokens"] == 2048


def test_api_key_provider_applies_the_declared_settings(monkeypatch) -> None:
    """`num_predict` était accepté, affiché, et jamais transmis.

    Le modèle refusait alors de répondre au-delà de la limite du fournisseur, en
    conseillant d'augmenter `max_tokens` — le réglage même qui était ignoré.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "clef")
    model = ProviderFactory(
        registry("api_key", temperature=0.2, num_predict=32000, timeout_seconds=600)
    ).build("provider")

    assert model.settings["max_tokens"] == 32000
    assert model.settings["timeout"] == 600
    assert model.settings["temperature"] == 0.2


def test_settings_left_unset_are_omitted(monkeypatch) -> None:
    """Le SDK distingue « non précisé » de « nul » : n'envoyer que le déclaré."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "clef")
    model = ProviderFactory(registry("api_key")).build("provider")

    assert "max_tokens" not in model.settings
    assert "temperature" not in model.settings


def test_api_key_is_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-from-environment")
    model = ProviderFactory(registry("api_key")).build("provider")
    assert isinstance(model, OpenAIChatModel)


def test_config_key_is_used_when_environment_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    model = ProviderFactory(registry("api_key", api_key="stored-key")).build("provider")
    assert isinstance(model, OpenAIChatModel)


def test_config_key_has_priority_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-environment-key")
    model = ProviderFactory(registry("api_key", api_key="stored-key")).build("provider")
    assert isinstance(model, OpenAIChatModel)


def test_missing_api_key_explains_both_sources(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(AuthenticationError, match="providers.json api_key"):
        ProviderFactory(registry("api_key")).build("provider")


def test_factory_uses_a_registered_adapter_boundary() -> None:
    marker = object()

    class Adapter:
        key = "api_key"

        def build(self, config, model_name, oauth):
            assert config.id == "provider"
            assert model_name == "model"
            return marker

    adapters = ProviderAdapterRegistry([Adapter()])
    assert ProviderFactory(registry("api_key"), adapters=adapters).build("provider") is marker


def test_adapter_registry_rejects_duplicate_keys() -> None:
    class Adapter:
        key = "local"

        def build(self, config, model_name, oauth):
            return object()

    with pytest.raises(ConfigurationError, match="duplicate provider adapter"):
        ProviderAdapterRegistry([Adapter(), Adapter()])


def test_codex_requests_are_explicitly_not_stored() -> None:
    class Credential:
        access = "access-token"

        def __getitem__(self, key: str) -> str:
            assert key == "account_id"
            return "account-id"

    class OAuth:
        def access_token(self, provider_id: str) -> Credential:
            assert provider_id == "openai-codex"
            return Credential()

    codex_registry = ProviderRegistry(
        default_provider="codex",
        providers=[
            {
                "id": "codex",
                "kind": "openai-codex",
                "connection_type": "auth",
                "model": "gpt-5.5",
            }
        ],
    )
    model = ProviderFactory(
        codex_registry,
        oauth=OAuth(),  # type: ignore[arg-type]
    ).build("codex")

    assert isinstance(model, OpenAIResponsesModel)
    assert model.settings["openai_store"] is False


@pytest.mark.asyncio
async def test_model_discovery_falls_back_to_configured_models(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    models, source, error = await ProviderFactory(
        registry("api_key", models=["model-pro"])
    ).list_models("provider")
    assert models == ["model", "model-pro"]
    assert source == "configured"
    assert "missing API key" in (error or "")


@pytest.mark.asyncio
async def test_local_model_discovery_scans_gguf_directory(tmp_path) -> None:
    (tmp_path / "b-model.gguf").write_bytes(b"")
    (tmp_path / "a-model.GGUF").write_bytes(b"")
    models, source, error = await ProviderFactory(
        registry("local", models_dir=str(tmp_path), port=8123)
    ).list_models("provider")
    assert models == ["a-model", "b-model"]
    assert source == "directory"
    assert error is None


@pytest.mark.asyncio
async def test_local_model_discovery_collapses_shards(tmp_path) -> None:
    (tmp_path / "model-00001-of-00002.gguf").write_bytes(b"")
    (tmp_path / "model-00002-of-00002.gguf").write_bytes(b"")
    models, source, error = await ProviderFactory(
        registry("local", models_dir=str(tmp_path), port=8123),
        runtime_dir=tmp_path / "runtime",
    ).list_models("provider")
    assert models == ["model"]
    assert source == "directory"
    assert error is None


@pytest.mark.asyncio
async def test_codex_model_discovery_uses_catalog_contract(monkeypatch) -> None:
    class Credential:
        access = "access-token"

        def __getitem__(self, key: str) -> str:
            assert key == "account_id"
            return "account-id"

    class OAuth:
        def access_token(self, provider_id: str) -> Credential:
            assert provider_id == "openai-codex"
            return Credential()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/backend-api/codex/models"
        assert request.url.params["client_version"] == "0.145.0"
        assert request.headers["chatgpt-account-id"] == "account-id"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-5.6-sol", "visibility": "list"},
                    {"slug": "gpt-5.6-terra", "visibility": "list"},
                    {"slug": "codex-auto-review", "visibility": "hide"},
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "agentic_kernel.providers.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("agentic_kernel.providers._codex_client_version", lambda: "0.145.0")
    codex_registry = ProviderRegistry(
        default_provider="codex",
        providers=[
            {
                "id": "codex",
                "kind": "openai-codex",
                "connection_type": "auth",
                "base_url": "https://chatgpt.com/backend-api/codex",
            }
        ],
    )
    models, source, error = await ProviderFactory(
        codex_registry,
        oauth=OAuth(),  # type: ignore[arg-type]
    ).list_models("codex")
    assert models == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert source == "live"
    assert error is None


def test_a_managed_server_always_publishes_its_metrics(tmp_path) -> None:
    """Sans `--metrics`, la vitesse d'écriture reste invisible.

    L'interface ne pouvait alors qu'afficher des tokens divisés par la durée du
    run — un nombre qui s'effondre quand le prompt grossit, sans que rien ne
    ralentisse, et qui a fait diagnostiquer une panne matérielle inexistante.
    """
    modeles = tmp_path / "models"
    modeles.mkdir()
    (modeles / "model.gguf").write_bytes(b"")
    factory = ProviderFactory(
        registry("local", port=8125, models_dir=str(modeles), num_ctx=65536),
        runtime_dir=tmp_path / "runtime",
    )

    manager = factory._managed_llama(factory.get_config("provider"))

    assert manager is not None
    assert "--metrics" in manager.server_args


def test_managed_llama_applies_structured_reasoning_settings(tmp_path) -> None:
    modeles = tmp_path / "models"
    modeles.mkdir()
    (modeles / "qwen.gguf").write_bytes(b"")
    factory = ProviderFactory(
        registry(
            "local",
            port=8125,
            models_dir=str(modeles),
            preserve_thinking=True,
            reasoning_budget=16384,
        ),
        runtime_dir=tmp_path / "runtime",
    )

    manager = factory._managed_llama(factory.get_config("provider"))

    assert manager is not None
    position = manager.server_args.index("--chat-template-kwargs")
    assert manager.server_args[position : position + 2] == [
        "--chat-template-kwargs",
        '{"preserve_thinking":true}',
    ]
    position = manager.server_args.index("--reasoning-budget")
    assert manager.server_args[position : position + 2] == [
        "--reasoning-budget",
        "16384",
    ]


def test_a_remote_provider_reports_no_throughput() -> None:
    """DeepSeek ne distingue pas la lecture du prompt de l'écriture : on n'invente pas."""
    factory = ProviderFactory(registry("api_key", api_key="k"))

    assert factory.throughput_counters("provider") is None
