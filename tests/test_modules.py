import json
from pathlib import Path

import pytest

from agentic_kernel.errors import ModuleError
from agentic_kernel.models import ToolRisk
from agentic_kernel.modules import ModuleRegistry


def test_index_is_deterministic_and_loadable(project: Path) -> None:
    registry = ModuleRegistry(project / "tools")
    first = registry.build_index()
    first_bytes = registry.index_path.read_bytes()
    second = registry.build_index()
    assert first == second
    assert registry.index_path.read_bytes() == first_bytes
    assert len(registry.load(["clock"])) == 1


def test_stale_index_is_rejected(project: Path) -> None:
    registry = ModuleRegistry(project / "tools")
    registry.build_index()
    data = json.loads(registry.index_path.read_text())
    data["modules"] = []
    registry.index_path.write_text(json.dumps(data))
    with pytest.raises(ModuleError, match="stale"):
        registry.check_index()


def test_missing_entrypoint_is_rejected(project: Path) -> None:
    registry = ModuleRegistry(project / "tools")
    (project / "tools/modules/clock/module.py").unlink()
    with pytest.raises(ModuleError, match="missing entrypoint"):
        registry.discover()


def test_every_manifest_matches_runtime_and_exposes_contract_metadata(project: Path) -> None:
    registry = ModuleRegistry(project / "tools")
    index = registry.build_index()
    registry.check_index()
    for manifest in index.modules:
        for tool in manifest.tools:
            assert tool.input_schema, tool.name
            assert tool.output_schema, tool.name
            assert tool.category
            assert tool.timeout_seconds is not None


def test_repository_catalog_passes_the_tool_result_contract() -> None:
    tools_root = Path(__file__).parents[1] / "tools"
    index = ModuleRegistry(tools_root).check_index()
    tools = [tool for manifest in index.modules for tool in manifest.tools]

    assert len(index.modules) == 18
    assert len(tools) >= 70
    for tool in tools:
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema["type"] == "object"
        assert {"ok", "data", "error", "metadata"} <= set(
            tool.output_schema["properties"]
        )
        assert tool.timeout_seconds is not None


def test_no_shipped_tool_declares_a_risk_that_denies_it_outright() -> None:
    """`secret` et `system` valent un refus inconditionnel du Guardian.

    Un outil qui les déclare est inutilisable dans tous les modes, y compris
    `power` — et rien ne le signale : le catalogue l'expose, l'agent l'appelle,
    le refus n'arrive qu'à l'exécution avec un message qui parle de sécurité
    plutôt que de configuration. Le coût s'est payé sur `sentinel_scan`, qui
    lisait l'état de la machine sans jamais la modifier.
    """
    tools_root = Path(__file__).parents[1] / "tools"
    index = ModuleRegistry(tools_root).check_index()

    fautifs = {
        tool.name: [risk.value for risk in tool.risk_tags]
        for manifest in index.modules
        for tool in manifest.tools
        if {ToolRisk.SECRET, ToolRisk.SYSTEM} & set(tool.risk_tags)
    }

    assert fautifs == {}
