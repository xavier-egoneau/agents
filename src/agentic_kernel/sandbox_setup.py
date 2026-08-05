from __future__ import annotations

import asyncio
import json
import platform
import shutil
from dataclasses import dataclass

from .errors import ConfigurationError
from .platform.processes import stop_async_process, subprocess_group_kwargs


@dataclass(frozen=True)
class SandboxSetupResult:
    mode: str
    success: bool
    detail: str


async def setup_windows_sandbox(timeout_seconds: float = 600) -> SandboxSetupResult:
    """Drive Codex app-server's official elevated Windows sandbox setup."""
    if platform.system() != "Windows":
        raise ConfigurationError("Windows sandbox setup is only available on Windows")
    codex = shutil.which("codex")
    if codex is None:
        raise ConfigurationError("Codex CLI is required to install the Windows sandbox helper")
    process = await asyncio.create_subprocess_exec(
        codex,
        "app-server",
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **subprocess_group_kwargs(),
    )
    assert process.stdin is not None
    assert process.stdout is not None

    async def send(message: dict[str, object]) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def receive() -> dict[str, object]:
        while line := await process.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                return message
        stderr = ""
        if process.stderr is not None:
            stderr = (await process.stderr.read()).decode(errors="replace").strip()
        raise ConfigurationError(stderr or "Codex app-server stopped during sandbox setup")

    async def protocol() -> SandboxSetupResult:
        await send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "agentic_markdown_kernel",
                        "title": "Agentic Markdown Kernel",
                        "version": "0.1.0",
                    }
                },
            }
        )
        while (message := await receive()).get("id") != 1:
            pass
        if "error" in message:
            raise ConfigurationError(f"Codex initialization failed: {message['error']}")
        await send({"method": "initialized", "params": {}})
        await send(
            {
                "method": "windowsSandbox/setupStart",
                "id": 2,
                "params": {"mode": "elevated"},
            }
        )
        started = False
        while True:
            message = await receive()
            if message.get("id") == 2:
                if "error" in message:
                    raise ConfigurationError(f"Sandbox setup failed to start: {message['error']}")
                result = message.get("result")
                started = isinstance(result, dict) and result.get("started") is True
                if not started:
                    raise ConfigurationError("Codex did not start the Windows sandbox setup")
                continue
            if message.get("method") != "windowsSandbox/setupCompleted":
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                raise ConfigurationError("Invalid completion response from Codex sandbox setup")
            success = params.get("success") is True
            error = params.get("error")
            return SandboxSetupResult(
                mode=str(params.get("mode") or "elevated"),
                success=success,
                detail="ready" if success else str(error or "setup failed"),
            )

    try:
        return await asyncio.wait_for(protocol(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise ConfigurationError("Windows sandbox setup timed out") from exc
    finally:
        if process.stdin is not None:
            process.stdin.close()
        await stop_async_process(process, 2)
