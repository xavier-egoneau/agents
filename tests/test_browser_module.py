from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agentic_kernel.events import JsonlEventStore


async def test_playwright_browser_snapshot_and_screenshot(tmp_path: Path) -> None:
    async def serve(reader, writer):
        await reader.read(4096)
        body = b"<html><title>AMK test</title><button>Bonjour</button></html>"
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
    source = Path(__file__).parents[1] / "tools/modules/browser/module.py"
    spec = importlib.util.spec_from_file_location("test_browser_tool_module", source)
    assert spec and spec.loader
    browser_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(browser_module)

    session_id, run_id = uuid4(), uuid4()
    events = JsonlEventStore(tmp_path / "sessions")
    ctx = SimpleNamespace(deps=SimpleNamespace(
        session_id=session_id,
        root_run_id=run_id,
        events=events,
    ))
    try:
        opened = await browser_module.browser_open(ctx, f"http://127.0.0.1:{port}")
        page_id = opened["data"]["page_id"]
        snapshot = await browser_module.browser_snapshot(ctx, page_id)
        screenshot = await browser_module.browser_screenshot(ctx, page_id)
        assert snapshot["data"]["title"] == "AMK test"
        assert snapshot["data"]["elements"][0]["text"] == "Bonjour"
        assert Path(screenshot["data"]["path"]).is_file()
        artifact = next(event for event in events.read(session_id) if event.type == "artifact.created")
        assert artifact.run_id == run_id
        assert artifact.payload["media_type"] == "image/png"
    finally:
        await browser_module.browser_close(ctx)
        server.close()
        await server.wait_closed()
