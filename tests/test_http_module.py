from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_http_module():
    path = Path(__file__).parents[1] / "tools" / "modules" / "http" / "module.py"
    spec = importlib.util.spec_from_file_location("test_amk_http_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_private_target_reuses_exact_http_request_scope(monkeypatch) -> None:
    module = load_http_module()
    target = "http://127.0.0.1/private"
    scope = module.network_scope(target)
    checked: list[tuple[str, bool]] = []

    async def validate_target(url: str, *, allow_private: bool = False) -> None:
        checked.append((url, allow_private))
        if not allow_private:
            raise module.NetworkTargetError("private target denied")

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            pass

        async def request(self, method: str, url: str, **kwargs):
            return SimpleNamespace(
                is_redirect=False,
                content=b"private response",
                encoding="utf-8",
                headers={},
                url=url,
                status_code=200,
            )

    monkeypatch.setattr(module, "validate_http_target", validate_target)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            workspace=Path.cwd(),
            approved_scopes={("http_request", "network", scope)},
        ),
        tool_call_approved=False,
    )

    result = await module.http_request(ctx, "GET", target)

    assert result["ok"] is True
    assert result["data"]["body"] == "private response"
    assert checked == [(target, True)]


@pytest.mark.parametrize(
    "approved_scopes",
    [
        {("network", "http://127.0.0.1:80")},
        {("*", "network", None)},
        {("web_scrape", "network", "http://127.0.0.1:80")},
        {("http_request", "network", "http://127.0.0.1:8080")},
    ],
)
async def test_private_target_rejects_non_exact_scopes(
    monkeypatch,
    approved_scopes: set[tuple],
) -> None:
    module = load_http_module()
    target = "http://127.0.0.1/private"
    checked: list[bool] = []

    async def validate_target(url: str, *, allow_private: bool = False) -> None:
        checked.append(allow_private)
        if not allow_private:
            raise module.NetworkTargetError("private target denied")

    monkeypatch.setattr(module, "validate_http_target", validate_target)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(workspace=Path.cwd(), approved_scopes=approved_scopes),
        tool_call_approved=False,
    )

    result = await module.http_request(ctx, "GET", target)

    assert result["ok"] is False
    assert result["error"]["type"] == "ssrf_blocked"
    assert checked == [False]
