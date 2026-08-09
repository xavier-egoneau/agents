from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse


class NetworkTargetError(ValueError):
    pass


def network_scope(url: str) -> str | None:
    """Return the exact scheme/host/port permission scope for one target."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname.casefold()}:{port}"


def validate_file_target(url: str, workspace: Path) -> Path:
    """Validate a file:// URL and ensure it's within the workspace.

    Returns the resolved file path if valid. Raises NetworkTargetError if the
    URL is invalid or the file is outside the workspace (security boundary).
    """
    from urllib.request import url2pathname

    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise NetworkTargetError(f"not a file:// URL: {url}")
    # Use url2pathname for proper cross-platform path extraction (handles
    # file:///C:/path on Windows and file:///path on Unix).
    file_path = Path(url2pathname(parsed.path)).expanduser().resolve()
    # Security: ensure the file is within the workspace
    workspace_resolved = workspace.resolve()
    try:
        file_path.relative_to(workspace_resolved)
    except ValueError:
        raise NetworkTargetError(
            f"file:// URL points outside workspace: {file_path} "
            f"(workspace: {workspace_resolved})"
        ) from None
    if not file_path.exists():
        raise NetworkTargetError(f"file not found: {file_path}")
    if not file_path.is_file():
        raise NetworkTargetError(f"not a regular file: {file_path}")
    return file_path


async def validate_http_target(url: str, *, allow_private: bool = False) -> None:
    """Resolve an HTTP target and reject credentials and private destinations."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkTargetError("only absolute HTTP(S) URLs are accepted")
    if parsed.username or parsed.password:
        raise NetworkTargetError("credentials in URLs are forbidden")
    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise NetworkTargetError(f"unresolved network target: {parsed.hostname}") from exc
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})
    if not addresses:
        raise NetworkTargetError(f"unresolved network target: {parsed.hostname}")
    private = any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    )
    if private and not allow_private:
        raise NetworkTargetError(
            "private, local, reserved, and unresolved targets require exact approval"
        )
