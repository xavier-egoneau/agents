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


def _capacite(window: int):
    from agentic_kernel.compaction import ContextWindowCompaction

    return ContextWindowCompaction(
        agent_id="main", context_window_tokens=window, all_tool_names=set()
    )


def test_compaction_triggered_too_late_without_calibration() -> None:
    """Le défaut observé : 19 compactions et une fenêtre saturée quand même.

    L'estimateur compte 3,5 octets par token; le fournisseur en a facturé 2,4
    fois plus sur une session de code. Comparer l'estimation brute au seuil
    faisait attendre le double du volume voulu, donc au-delà de la fenêtre.
    """
    capacite = _capacite(1_000_000)
    # 500 000 tokens estimés : la moitié de la fenêtre en apparence, donc
    # tranquillement sous le seuil de 700 000.
    estime = 500_000

    assert capacite.should_compact(estime, 1_000_000, 1.0) is False
    # Le facteur relevé sur la session en cause est de 2,43 : ces 500 000
    # tokens en valent 1 215 000 chez le fournisseur, soit plus que la fenêtre
    # entière. La compaction doit avoir eu lieu bien avant.
    assert capacite.should_compact(estime, 1_000_000, 2.43) is True


def test_calibration_never_widens_the_apparent_margin() -> None:
    """Un facteur sous 1 ferait croire à plus de place qu'il n'y en a."""
    capacite = _capacite(100_000)

    assert capacite.calibration_of(type("D", (), {"context_calibration": 0.2})()) == 1.0
    assert capacite.calibration_of(type("D", (), {"context_calibration": None})()) == 1.0
    assert capacite.calibration_of(object()) == 1.0
    assert capacite.calibration_of(type("D", (), {"context_calibration": 2.4})()) == 2.4


def test_a_manual_compaction_ignores_the_threshold() -> None:
    from agentic_kernel.compaction import ContextWindowCompaction

    forcee = ContextWindowCompaction(
        agent_id="main", context_window_tokens=1_000_000, all_tool_names=set(), force=True
    )

    assert forcee.should_compact(10, 1_000_000, 1.0) is True


def test_an_unknown_window_cannot_trigger_a_threshold() -> None:
    assert _capacite(0).should_compact(10_000_000, None, 3.0) is False
