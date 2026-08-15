from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_mcp_module():
    path = Path(__file__).parents[1] / "tools" / "modules" / "mcp" / "module.py"
    spec = importlib.util.spec_from_file_location("test_amk_mcp_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ctx(tool_call_approved: bool = False, approved_scopes: set | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        deps=SimpleNamespace(approved_scopes=approved_scopes or set()),
        tool_call_approved=tool_call_approved,
    )


async def test_private_mcp_server_target_is_blocked(monkeypatch) -> None:
    """mcp.json est une configuration locale, pas une autorisation réseau.

    Sans vérification, une URL intranet configurée là était jointe telle
    quelle : la classification du Guardian ne résout pas le DNS.
    """
    module = load_mcp_module()
    monkeypatch.setattr(
        module,
        "_config",
        lambda ctx: {"nas": {"transport": "http", "url": "http://127.0.0.1:9"}},
    )

    result = await module.mcp_search(_ctx(), "outil", server_id="nas")

    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert "blocked" in result["error"]["message"]


async def test_mcp_target_approved_by_scope_proceeds(monkeypatch) -> None:
    """La vérification suit le même contrat que http_request : portée exacte."""
    module = load_mcp_module()
    monkeypatch.setattr(
        module,
        "_config",
        lambda ctx: {"local": {"transport": "http", "url": "http://127.0.0.1:9"}},
    )

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            pass

        async def post(self, url: str, **kwargs):
            return SimpleNamespace(
                json=lambda: {"result": {"tools": [{"name": "outil", "description": "d"}]}},
                raise_for_status=lambda: None,
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    ctx = _ctx(
        tool_call_approved=True,
    )

    result = await module.mcp_search(ctx, "outil", server_id="local")

    assert result["ok"] is True
    assert result["data"][0]["name"] == "outil"
