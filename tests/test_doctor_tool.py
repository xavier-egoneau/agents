from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from agentic_kernel.doctor import Diagnostic
from agentic_kernel.paths import RuntimeLayout


def _load_doctor_module():
    source = Path(__file__).parents[1] / "tools/modules/doctor/module.py"
    spec = importlib.util.spec_from_file_location("test_doctor_tool_module", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_doctor_classifies_failures_warnings_and_optional_checks(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_doctor_module()
    layout = RuntimeLayout(
        application_root=tmp_path,
        content_root=tmp_path / "content-agents",
        workspace=tmp_path / "workspace",
    )
    checks = [
        Diagnostic("application", "ok", str(tmp_path), True),
        Diagnostic("tool catalog", "error", "index is stale", True),
        Diagnostic("sandbox", "limited", "Docker unavailable"),
        Diagnostic("Gemma 4 model", "optional", "download on first use"),
    ]
    monkeypatch.setattr(tool, "runtime_layout", lambda _workspace: layout)
    monkeypatch.setattr(tool, "diagnose", lambda _layout: checks)
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=layout.workspace))

    result = await tool.doctor(ctx, justification="A tool is unavailable")

    assert result["ok"] is True
    assert result["data"]["status"] == "unhealthy"
    assert result["data"]["healthy"] is False
    assert [item["name"] for item in result["data"]["failures"]] == ["tool catalog"]
    assert [item["name"] for item in result["data"]["warnings"]] == ["sandbox"]
    assert [item["name"] for item in result["data"]["optional"]] == ["Gemma 4 model"]
    assert result["metadata"]["content_root"] == str(layout.content_root)


async def test_doctor_is_healthy_when_every_required_check_passes(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _load_doctor_module()
    layout = RuntimeLayout(tmp_path, tmp_path / "content-agents", tmp_path)
    monkeypatch.setattr(tool, "runtime_layout", lambda _workspace: layout)
    monkeypatch.setattr(
        tool,
        "diagnose",
        lambda _layout: [Diagnostic("application", "ok", str(tmp_path), True)],
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=tmp_path))

    result = await tool.doctor(ctx)

    assert result["data"]["status"] == "healthy"
    assert result["data"]["healthy"] is True
    assert result["data"]["failures"] == []
    assert result["data"]["warnings"] == []


def test_every_bundled_agent_can_access_doctor() -> None:
    agents = Path(__file__).parents[1] / "src/agentic_kernel/defaults/agents"

    for name in ("main", "dev", "reviewer", "researcher", "uifront"):
        source = (agents / f"{name}.md").read_text(encoding="utf-8")
        assert "\n  - doctor\n" in source, name
