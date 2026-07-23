import os
from pathlib import Path

import pytest
from pydantic_ai import Agent

from agentic_kernel.config import ProjectConfig
from agentic_kernel.providers import ProviderFactory

PROVIDERS = ("llama-local", "deepseek", "openai-codex", "claude")


@pytest.mark.integration
@pytest.mark.parametrize("provider_id", PROVIDERS)
async def test_live_provider_round_trip(provider_id: str) -> None:
    """Opt-in smoke test: AMK_RUN_LIVE_TESTS=1 may incur provider usage."""
    if os.getenv("AMK_RUN_LIVE_TESTS") != "1":
        pytest.skip("set AMK_RUN_LIVE_TESTS=1 to call real providers")
    factory = ProviderFactory(ProjectConfig(Path.cwd()).providers())
    result = await Agent(factory.build(provider_id)).run("Reply with exactly: OK")
    assert "OK" in str(result.output).upper()
