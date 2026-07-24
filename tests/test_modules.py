import json
from pathlib import Path

import pytest

from agentic_kernel.errors import ModuleError
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
