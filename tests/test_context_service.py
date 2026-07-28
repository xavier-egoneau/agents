import json
from pathlib import Path

import pytest

from agentic_kernel.context_service import ModelContextRegistry
from agentic_kernel.errors import ConfigurationError


def test_context_registry_parses_human_sizes() -> None:
    assert ModelContextRegistry.parse_size("128k") == 128_000
    assert ModelContextRegistry.parse_size("1.5m") == 1_500_000
    assert ModelContextRegistry.parse_size("32_768") == 32_768


@pytest.mark.parametrize("value", ["", "unknown", "100", "20m"])
def test_context_registry_rejects_invalid_sizes(value: str) -> None:
    with pytest.raises(ValueError):
        ModelContextRegistry.parse_size(value)


def test_context_registry_replaces_one_model_without_losing_others(tmp_path: Path) -> None:
    registry = ModelContextRegistry(tmp_path)
    registry.set("deepseek", "chat", 64_000)
    registry.set("openai", "codex", 200_000)
    registry.set("deepseek", "chat", 128_000)

    assert registry.get("deepseek", "chat") == 128_000
    assert registry.get("openai", "codex") == 200_000
    document = json.loads((tmp_path / "models-infos.json").read_text(encoding="utf-8"))
    assert len(document["models"]) == 2


def test_context_registry_reports_invalid_document(tmp_path: Path) -> None:
    (tmp_path / "models-infos.json").write_text("{", encoding="utf-8")
    registry = ModelContextRegistry(tmp_path)
    with pytest.raises(ConfigurationError, match="registre invalide"):
        registry.set("deepseek", "chat", 128_000)
