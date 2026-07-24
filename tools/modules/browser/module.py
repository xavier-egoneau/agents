from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, Page, Playwright, async_playwright
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.models import Event

MAX_TEXT = 50_000
MAX_ELEMENTS = 200
SESSION_TTL_SECONDS = 30 * 60


class BrowserSession:
    def __init__(self, playwright: Playwright, browser: Browser) -> None:
        self.playwright = playwright
        self.browser = browser
        self.pages: dict[str, Page] = {}
        self.refs: dict[str, dict[str, str]] = {}
        self.touched_at = time.monotonic()


_sessions: dict[str, BrowserSession] = {}
_lock = asyncio.Lock()


def _result(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": metadata}


def _failure(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"type": kind, "message": message}, "metadata": {}}


async def _expire() -> None:
    expired = [
        key for key, value in _sessions.items()
        if time.monotonic() - value.touched_at > SESSION_TTL_SECONDS
    ]
    for key in expired:
        await _close_session(key)


async def _session(ctx: RunContext[Any]) -> BrowserSession:
    key = str(ctx.deps.session_id)
    async with _lock:
        await _expire()
        current = _sessions.get(key)
        if current is None:
            playwright = await async_playwright().start()
            try:
                try:
                    browser = await playwright.chromium.launch(
                        channel="chrome",
                        headless=True,
                        args=["--disable-background-networking"],
                    )
                except Exception:
                    browser = await playwright.chromium.launch(
                        headless=True,
                        args=["--disable-background-networking"],
                    )
            except Exception:
                await playwright.stop()
                raise
            current = BrowserSession(playwright=playwright, browser=browser)
            _sessions[key] = current
        current.touched_at = time.monotonic()
        return current


async def _close_session(key: str) -> None:
    current = _sessions.pop(key, None)
    if current is None:
        return
    try:
        await current.browser.close()
    finally:
        await current.playwright.stop()


async def _page(ctx: RunContext[Any], page_id: str) -> tuple[BrowserSession, Page]:
    current = await _session(ctx)
    page = current.pages.get(page_id)
    if page is None or page.is_closed():
        raise ValueError(f"unknown browser page: {page_id}")
    return current, page


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("browser accepts only absolute HTTP(S) URLs")
    if parsed.username or parsed.password:
        raise ValueError("credentials in browser URLs are forbidden")


async def browser_open(
    ctx: RunContext[Any],
    url: str,
    page_id: str | None = None,
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "domcontentloaded",
    justification: str = "",
) -> dict[str, Any]:
    """Open a rendered JavaScript page or navigate an existing browser page."""
    _validate_url(url)
    current = await _session(ctx)
    if page_id:
        page = current.pages.get(page_id)
        if page is None or page.is_closed():
            return _failure("not_found", f"unknown browser page: {page_id}")
    else:
        page_id = str(uuid4())
        context = await current.browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="fr-FR",
            color_scheme="light",
        )
        page = await context.new_page()
        current.pages[page_id] = page
    response = await page.goto(url, wait_until=wait_until, timeout=60_000)
    return _result({
        "page_id": page_id,
        "url": page.url,
        "title": await page.title(),
        "status_code": response.status if response else None,
    })


async def browser_snapshot(
    ctx: RunContext[Any], page_id: str, justification: str = ""
) -> dict[str, Any]:
    """Return bounded visible text plus stable references to interactive elements."""
    current, page = await _page(ctx, page_id)
    body = await page.locator("body").inner_text(timeout=15_000)
    locator = page.locator(
        "a,button,input,textarea,select,[role=button],[role=link],[contenteditable=true]"
    )
    count = min(await locator.count(), MAX_ELEMENTS)
    elements: list[dict[str, Any]] = []
    refs: dict[str, str] = {}
    for index in range(count):
        item = locator.nth(index)
        if not await item.is_visible():
            continue
        ref = f"e{len(elements)}"
        selector = (
            ":nth-match(:is(a,button,input,textarea,select,"
            f"[role=button],[role=link],[contenteditable=true]), {index + 1})"
        )
        refs[ref] = selector
        elements.append({
            "ref": ref,
            "tag": await item.evaluate("(el) => el.tagName.toLowerCase()"),
            "text": (await item.inner_text() if await item.evaluate(
                "(el) => !['INPUT','TEXTAREA','SELECT'].includes(el.tagName)"
            ) else await item.get_attribute("aria-label") or await item.get_attribute("placeholder") or "")[:300],
            "name": await item.get_attribute("name"),
            "type": await item.get_attribute("type"),
        })
    current.refs[page_id] = refs
    return _result({
        "page_id": page_id,
        "url": page.url,
        "title": await page.title(),
        "text": body[:MAX_TEXT],
        "elements": elements,
    }, truncated=len(body) > MAX_TEXT)


def _selector(current: BrowserSession, page_id: str, selector_or_ref: str) -> str:
    return current.refs.get(page_id, {}).get(selector_or_ref, selector_or_ref)


async def browser_click(
    ctx: RunContext[Any],
    page_id: str,
    selector_or_ref: str,
    justification: str = "",
) -> dict[str, Any]:
    """Click one visible element by snapshot reference or CSS selector."""
    current, page = await _page(ctx, page_id)
    selector = _selector(current, page_id, selector_or_ref)
    await page.locator(selector).click(timeout=30_000)
    await page.wait_for_timeout(250)
    return _result({"page_id": page_id, "url": page.url, "title": await page.title()})


async def browser_type(
    ctx: RunContext[Any],
    page_id: str,
    selector_or_ref: str,
    text: str,
    submit: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Fill one field by snapshot reference or CSS selector and optionally submit."""
    current, page = await _page(ctx, page_id)
    selector = _selector(current, page_id, selector_or_ref)
    target = page.locator(selector)
    await target.fill(text, timeout=30_000)
    if submit:
        await target.press("Enter")
        await page.wait_for_timeout(500)
    return _result({"page_id": page_id, "url": page.url, "submitted": submit})


async def browser_screenshot(
    ctx: RunContext[Any],
    page_id: str,
    full_page: bool = True,
    name: str | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Capture a rendered page and expose it as a conversation image artifact."""
    _, page = await _page(ctx, page_id)
    artifact_id = str(uuid4())
    safe_name = Path(name or f"browser-{artifact_id}.png").name
    if not safe_name.casefold().endswith(".png"):
        safe_name += ".png"
    directory = (
        ctx.deps.events.directory / "artifacts" / str(ctx.deps.session_id)
        / str(ctx.deps.root_run_id)
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{artifact_id}-{safe_name}"
    await page.screenshot(path=str(target), full_page=full_page, type="png")
    payload = {
        "artifact_id": artifact_id,
        "name": safe_name,
        "media_type": "image/png",
        "kind": "image",
        "path": str(target),
        "bytes": target.stat().st_size,
        "url": page.url,
    }
    ctx.deps.events.append(Event(
        session_id=ctx.deps.session_id,
        run_id=ctx.deps.root_run_id,
        agent_id="browser",
        type="artifact.created",
        payload=payload,
    ))
    return _result(payload)


async def browser_close(
    ctx: RunContext[Any],
    page_id: str | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Close one page, or every browser resource held by this session."""
    key = str(ctx.deps.session_id)
    current = _sessions.get(key)
    if current is None:
        return _result({"closed": False})
    if page_id:
        page = current.pages.pop(page_id, None)
        current.refs.pop(page_id, None)
        if page is None:
            return _failure("not_found", f"unknown browser page: {page_id}")
        context = page.context
        await page.close()
        await context.close()
        return _result({"closed": True, "page_id": page_id})
    await _close_session(key)
    return _result({"closed": True, "session": key})


class BrowserModule:
    def toolsets(self):
        return [FunctionToolset(tools=[
            browser_open, browser_snapshot, browser_click, browser_type,
            browser_screenshot, browser_close,
        ])]

    def instructions(self):
        return [
            "Use browser tools when JavaScript rendering or interaction is required. "
            "Call browser_snapshot before clicking. Use browser_screenshot when the user "
            "asks to see the rendered result; screenshot artifacts appear in the conversation."
        ]

    def capabilities(self):
        return []


module = BrowserModule()
