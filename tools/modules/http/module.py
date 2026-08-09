from __future__ import annotations

import os
from typing import Annotated, Any, Literal
from urllib.parse import urljoin

import httpx
from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.network_policy import (
    NetworkTargetError,
    network_scope,
    validate_http_target,
)

MAX_RESPONSE_BYTES = 200_000
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
Method = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


def _failure(kind: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"type": kind, "message": message},
        "metadata": metadata,
    }


async def http_request(  # noqa: C901 - dette: requête multi-cas
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
    initial_scope = network_scope(url)
    approved_scopes = getattr(ctx.deps, "approved_scopes", set())

    def private_allowed(target: str) -> bool:
        scope = network_scope(target)
        return bool(
            scope
            and (
                ("http_request", "network", scope) in approved_scopes
                or (bool(getattr(ctx, "tool_call_approved", False)) and scope == initial_scope)
            )
        )

    try:
        await validate_http_target(url, allow_private=private_allowed(url))
    except (NetworkTargetError, OSError) as exc:
        return _failure("ssrf_blocked", str(exc))
    request_headers = dict(headers or {})
    if any(name.casefold() in SENSITIVE_HEADERS for name in request_headers):
        return _failure("validation", "sensitive headers require credential_env")
    if credential_env:
        resolver = getattr(ctx.deps, "secret_resolver", None)
        token = resolver(credential_env) if callable(resolver) else None
        token = token or os.getenv(credential_env)
        if not token:
            return _failure(
                "authentication", f"credential reference is unavailable: {credential_env}"
            )
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
                        return _failure(
                            "redirect", "maximum redirects exceeded", redirects=redirects
                        )
                    current_url = urljoin(current_url, response.headers["location"])
                    await validate_http_target(
                        current_url,
                        allow_private=private_allowed(current_url),
                    )
                    redirects += 1
                    continue
                raw = response.content
                body = raw[:MAX_RESPONSE_BYTES].decode(
                    response.encoding or "utf-8", errors="replace"
                )
                public_headers = {
                    name: value
                    for name, value in response.headers.items()
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
