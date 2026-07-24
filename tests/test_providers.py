import httpx
import pytest
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from agentic_kernel.errors import AuthenticationError
from agentic_kernel.models import ProviderRegistry
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
