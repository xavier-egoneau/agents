from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

MAX_RESPONSE_BYTES = 200_000
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
Method = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


def _failure(kind: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"type": kind, "message": message}, "metadata": metadata}


async def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only absolute HTTP(S) URLs are accepted")
    if parsed.username or parsed.password:
        raise ValueError("credentials in URLs are forbidden")
    if parsed.hostname.casefold() == "localhost" or parsed.hostname.casefold().endswith(".local"):
        raise ValueError("local network targets are forbidden")
    try:
        direct = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        direct = list({ipaddress.ip_address(record[4][0]) for record in records})
    if not direct or any(
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
        for address in direct
    ):
        raise ValueError("private, local, reserved, and unresolved targets are forbidden")


async def http_request(
    ctx: RunContext[Any],
    method: Method,
    url: str,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    text_body: str | None = None,
    credential_env: str | None = None,
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30,
    max_redirects: Annotated[int, Field(ge=0, le=5)] = 3,
    justification: str = "",
) -> dict[str, Any]:
    """Perform one bounded public HTTP(S) request without exposing credentials."""
    try:
        await _validate_public_url(url)
    except (ValueError, OSError, socket.gaierror) as exc:
        return _failure("ssrf_blocked", str(exc))
    request_headers = dict(headers or {})
    if any(name.casefold() in SENSITIVE_HEADERS for name in request_headers):
        return _failure("validation", "sensitive headers require credential_env")
    if credential_env:
        resolver = getattr(ctx.deps, "secret_resolver", None)
        token = resolver(credential_env) if callable(resolver) else None
        token = token or os.getenv(credential_env)
        if not token:
            return _failure("authentication", f"credential reference is unavailable: {credential_env}")
        request_headers["Authorization"] = f"Bearer {token}"
    if json_body is not None and text_body is not None:
        return _failure("validation", "json_body and text_body are mutually exclusive")

    current_url, redirects = url, 0
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            while True:
                response = await client.request(
                    method,
                    current_url,
                    headers=request_headers,
                    params=query,
                    json=json_body,
                    content=text_body,
                )
                if response.is_redirect and response.headers.get("location"):
                    if redirects >= max_redirects:
                        return _failure("redirect", "maximum redirects exceeded", redirects=redirects)
                    current_url = urljoin(current_url, response.headers["location"])
                    await _validate_public_url(current_url)
                    redirects += 1
                    continue
                raw = response.content
                body = raw[:MAX_RESPONSE_BYTES].decode(
                    response.encoding or "utf-8", errors="replace"
                )
                public_headers = {
                    name: value for name, value in response.headers.items()
                    if name.casefold() not in SENSITIVE_HEADERS
                }
                return {
                    "ok": True,
                    "data": {
                        "status_code": response.status_code,
                        "url": str(response.url),
                        "headers": public_headers,
                        "body": body,
                    },
                    "error": None,
                    "metadata": {
                        "redirects": redirects,
                        "bytes": len(raw),
                        "truncated": len(raw) > MAX_RESPONSE_BYTES,
                    },
                }
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return _failure("transport", str(exc), redirects=redirects)


class HttpModule:
    def toolsets(self):
        return [FunctionToolset(tools=[http_request])]

    def instructions(self):
        return [
            "Use http_request for structured public HTTP(S) API calls. "
            "Use credential_env references instead of passing secrets."
        ]

    def capabilities(self):
        return []


module = HttpModule()
