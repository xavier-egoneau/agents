from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module():
    path = Path("tools/modules/codegraph/module.py").resolve()
    spec = importlib.util.spec_from_file_location("test_codegraph_tool_module", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_codegraph_reports_missing_cli(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_executable", lambda: None)
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=tmp_path))

    result = await module.codegraph_explore(ctx, "entrypoint")

    assert result["ok"] is False
    assert result["error"]["type"] == "dependency_missing"


@pytest.mark.asyncio
async def test_codegraph_initializes_then_explores(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "_executable", lambda: "/bin/codegraph")

    async def fake_subprocess(argv, workspace, timeout, env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="result", stderr="")

    monkeypatch.setattr(module, "_subprocess", fake_subprocess)
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=tmp_path))

    result = await module.codegraph_explore(ctx, "authentication flow")

    assert result["ok"] is True
    assert calls == [
        ["/bin/codegraph", "init", "."],
        ["/bin/codegraph", "explore", "authentication flow"],
    ]
    assert result["data"]["stdout"] == "result"


@pytest.mark.asyncio
async def test_codegraph_syncs_existing_index(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    (tmp_path / ".codegraph").mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "_executable", lambda: "/bin/codegraph")

    async def fake_subprocess(argv, workspace, timeout, env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(module, "_subprocess", fake_subprocess)
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=tmp_path))

    result = await module.codegraph_status(ctx)
    explored = await module.codegraph_query(ctx, "Kernel")

    assert result["ok"] is True
    assert explored["ok"] is True
    assert calls == [
        ["/bin/codegraph", "status", ".", "--json"],
        ["/bin/codegraph", "sync", "."],
        ["/bin/codegraph", "query", "Kernel", "--limit", "20", "--json"],
    ]
