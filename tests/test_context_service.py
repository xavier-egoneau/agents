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


def test_the_internal_catalog_is_not_counted_as_prompt(project: Path) -> None:
    """L'overhead ne doit compter que ce qui part vers le modèle.

    `tools/index.json` est un catalogue interne — sources, catégories, schémas
    de sortie, métadonnées — et pesait 246 ko chez un utilisateur, soit 70 500
    tokens. Le compter en entier plaçait l'occupation au-dessus du seuil de
    compaction avant même le premier message, rendait la cible de réduction
    négative donc inatteignable, et faisait payer à chaque run un instantané et
    une passe de compaction qui ne réduisaient rien.

    Ce qui part réellement, c'est le nom, la description et le schéma d'entrée
    de chaque outil exposé.
    """
    from agentic_kernel.kernel import Kernel

    kernel = Kernel(project)
    remplissage = {"type": "object", "properties": {f"champ_{i}": {"type": "string",
                   "description": "x" * 200} for i in range(60)}}
    kernel.module_registry.index_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "modules": [
                    {
                        "schema_version": 1,
                        "id": "clock",
                        "name": "Clock",
                        "description": "Test module",
                        "version": "1.0.0",
                        "entrypoint": "module.py:module",
                        "capabilities": ["tools"],
                        "enabled": True,
                        "config": None,
                        "tools": [
                            {
                                "name": "now",
                                "description": "Return the time.",
                                "category": "test",
                                "risk_tags": ["read"],
                                "timeout_seconds": 5,
                                "input_schema": {"type": "object", "properties": {}},
                                "output_schema": remplissage,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalogue = kernel.context.estimate_tokens(
        kernel.module_registry.index_path.read_text(encoding="utf-8")
    )
    agent = kernel.config.agents()["main"]

    overhead = kernel.context.overhead_tokens(agent, {}, "", project)

    # Le schéma de sortie, que le modèle ne voit jamais, domine le fichier.
    assert catalogue > 3_000
    assert overhead < catalogue / 3
    # Mais l'outil exposé pèse : l'ignorer ferait sous-estimer dans l'autre sens.
    assert overhead > kernel.context.estimate_tokens(
        kernel.config.system_instructions() + agent.instructions
    )


def test_a_file_read_can_be_reclaimed_from_the_context() -> None:
    """Sans `read`, la compaction n'avait rien à récupérer dans une session de code.

    Les résultats de `read` sont ce qui remplit le contexte quand on travaille
    sur du code. Ils étaient pourtant protégés, alors qu'ils remplissent le
    critère annoncé : une observation déterministe, en lecture seule, qui rend
    le même contenu si on la rejoue. Sur une session observée, douze compactions
    d'affilée n'ont rien réduit et l'historique est monté à 130 000 tokens pour
    une fenêtre de 65 536.
    """
    from agentic_kernel.compaction import ContextWindowCompaction

    capacite = ContextWindowCompaction(
        agent_id="main",
        context_window_tokens=65_536,
        all_tool_names={"read", "write", "patch", "command_run", "list", "stat"},
    )

    assert "read" not in capacite.protected_tools
    # Les mutations et les sorties non reproductibles restent protégées.
    assert {"write", "patch", "command_run"} <= capacite.protected_tools
