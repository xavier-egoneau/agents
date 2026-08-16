from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import SecurityMode


def load_browser_module():
    source = Path(__file__).parents[1] / "tools/modules/browser/module.py"
    spec = importlib.util.spec_from_file_location("test_browser_tool_module", source)
    assert spec and spec.loader
    browser_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(browser_module)
    return browser_module


async def test_power_mode_honors_guardian_allow_for_exact_loopback(tmp_path: Path) -> None:
    async def serve(reader, writer):
        await reader.read(4096)
        body = b"<html><title>Power loopback</title><p>ready</p></html>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    import asyncio

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    browser_module = load_browser_module()
    session_id, run_id = uuid4(), uuid4()
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            session_id=session_id,
            root_run_id=run_id,
            workspace=tmp_path,
            events=JsonlEventStore(tmp_path / "sessions"),
            approved_scopes=set(),
            security_mode=SecurityMode.POWER,
        ),
        tool_call_approved=False,
    )
    try:
        opened = await browser_module.browser_open(ctx, f"http://127.0.0.1:{port}")
        assert opened["ok"] is True
        assert opened["data"]["title"] == "Power loopback"
    finally:
        await browser_module.browser_close(ctx)
        server.close()
        await server.wait_closed()


async def test_playwright_browser_snapshot_and_screenshot(tmp_path: Path) -> None:
    async def serve(reader, writer):
        await reader.read(4096)
        body = b"""<html><title>AMK test</title><button>Bonjour</button>
        <canvas id='game' width='64' height='64'></canvas><script>
        const c=document.querySelector('canvas').getContext('2d');
        c.fillStyle='red';c.fillRect(0,0,32,64);c.fillStyle='blue';c.fillRect(32,0,32,64);
        </script></html>"""
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    import asyncio

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    browser_module = load_browser_module()

    session_id, run_id = uuid4(), uuid4()
    events = JsonlEventStore(tmp_path / "sessions")
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            session_id=session_id,
            root_run_id=run_id,
            events=events,
            approved_scopes={("browser_open", "network", f"http://127.0.0.1:{port}")},
        ),
        tool_call_approved=False,
    )
    try:
        opened = await browser_module.browser_open(ctx, f"http://127.0.0.1:{port}")
        page_id = opened["data"]["page_id"]
        snapshot = await browser_module.browser_snapshot(ctx, page_id)
        screenshot = await browser_module.browser_screenshot(ctx, page_id)
        assert snapshot["data"]["title"] == "AMK test"
        assert snapshot["data"]["elements"][0]["text"] == "Bonjour"
        # Le chemin complet dépasse MAX_PATH sous Windows : un `is_file()` nu
        # répond False alors que le fichier existe, faute de préfixe `\\?\`.
        from agentic_kernel.platform.secure_files import long_path

        assert long_path(Path(screenshot["data"]["path"])).is_file()
        artifact = next(
            event for event in events.read(session_id) if event.type == "artifact.created"
        )
        assert artifact.run_id == run_id
        assert artifact.payload["media_type"] == "image/png"
    finally:
        await browser_module.browser_close(ctx)
        server.close()
        await server.wait_closed()


async def test_redirect_guard_requires_exact_browser_open_scope(monkeypatch) -> None:
    browser_module = load_browser_module()
    initial_url = "http://127.0.0.1:8000/start"
    redirected_url = "http://127.0.0.1:8001/redirected"
    redirected_scope = browser_module.network_scope(redirected_url)

    async def validate_target(url: str, *, allow_private: bool = False) -> None:
        if not allow_private:
            raise browser_module.NetworkTargetError("private target denied")

    class FakeContext:
        handler = None

        async def route(self, pattern: str, handler) -> None:
            self.handler = handler

    class FakeRoute:
        def __init__(self, url: str) -> None:
            self.request = SimpleNamespace(url=url)
            self.aborted: str | None = None
            self.continued = False

        async def abort(self, reason: str) -> None:
            self.aborted = reason

        async def continue_(self) -> None:
            self.continued = True

    monkeypatch.setattr(browser_module, "validate_http_target", validate_target)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            approved_scopes={("browser_open", "network", redirected_scope)},
        ),
        tool_call_approved=False,
    )
    context = FakeContext()
    await browser_module._guard_requests(
        context,
        ctx,
        initial_scope=browser_module.network_scope(initial_url),
    )
    assert context.handler is not None

    exact_route = FakeRoute(redirected_url)
    await context.handler(exact_route)
    assert exact_route.continued is True
    assert exact_route.aborted is None

    ctx.deps.approved_scopes = {("*", "network", None)}
    wildcard_route = FakeRoute(redirected_url)
    await context.handler(wildcard_route)
    assert wildcard_route.continued is False
    assert wildcard_route.aborted == "blockedbyclient"


async def test_browser_open_allows_workspace_file_and_local_assets(tmp_path: Path) -> None:
    browser_module = load_browser_module()
    (tmp_path / "style.css").write_text("body { background: rgb(1, 2, 3); }", encoding="utf-8")
    page = tmp_path / "index.html"
    page.write_text(
        '<html><head><title>Local</title><link rel="stylesheet" href="style.css"></head>'
        '<body><p id="ready">chargé</p></body></html>',
        encoding="utf-8",
    )
    session_id, run_id = uuid4(), uuid4()
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            session_id=session_id,
            root_run_id=run_id,
            workspace=tmp_path,
            events=JsonlEventStore(tmp_path / "sessions"),
            approved_scopes=set(),
        ),
        tool_call_approved=False,
    )
    try:
        opened = await browser_module.browser_open(ctx, page.as_uri())
        snapshot = await browser_module.browser_snapshot(ctx, opened["data"]["page_id"])

        assert opened["ok"] is True
        assert opened["data"]["title"] == "Local"
        assert snapshot["data"]["text"] == "chargé"
    finally:
        await browser_module.browser_close(ctx)
