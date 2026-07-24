from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.vision import LocalVisionService, VisionUnavailable

MAX_TEXT_BYTES = 200_000


def _path(ctx: RunContext[Any], raw: str) -> Path:
    candidate = Path(raw).expanduser()
    return (
        (ctx.deps.workspace / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )


def _failure(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"type": kind, "message": message}, "metadata": {}}


async def _command(command: list[str], timeout: float = 60) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.terminate()
        await process.wait()
        return 124, b"", b"local extractor timed out"
    return process.returncode or 0, stdout, stderr


async def pdf_extract(
    ctx: RunContext[Any],
    path: str,
    first_page: Annotated[int, Field(ge=1)] = 1,
    last_page: Annotated[int | None, Field(ge=1)] = None,
    justification: str = "",
) -> dict[str, Any]:
    """Extract bounded text from a PDF using a local stateless extractor."""
    binary = shutil.which("pdftotext")
    if not binary:
        return _failure("precondition", "pdftotext is not installed")
    target = _path(ctx, path)
    command = [binary, "-f", str(first_page)]
    if last_page is not None:
        command.extend(["-l", str(last_page)])
    command.extend([str(target), "-"])
    code, stdout, stderr = await _command(command)
    if code:
        return _failure("execution", stderr.decode(errors="replace")[:4000])
    raw = stdout[:MAX_TEXT_BYTES]
    return {
        "ok": True,
        "data": {"path": str(target), "text": raw.decode(errors="replace")},
        "error": None,
        "metadata": {"bytes": len(stdout), "truncated": len(stdout) > MAX_TEXT_BYTES},
    }


async def ocr_extract(
    ctx: RunContext[Any],
    path: str,
    language: str = "eng",
    justification: str = "",
) -> dict[str, Any]:
    """Extract bounded text from an image using local Tesseract."""
    binary = shutil.which("tesseract")
    if not binary:
        return _failure("precondition", "tesseract is not installed")
    target = _path(ctx, path)
    code, stdout, stderr = await _command([binary, str(target), "stdout", "-l", language])
    if code:
        return _failure("execution", stderr.decode(errors="replace")[:4000])
    return {
        "ok": True,
        "data": {"path": str(target), "text": stdout[:MAX_TEXT_BYTES].decode(errors="replace")},
        "error": None,
        "metadata": {"bytes": len(stdout), "truncated": len(stdout) > MAX_TEXT_BYTES},
    }


async def image_inspect(
    ctx: RunContext[Any],
    path: str,
    question: str = "Décris précisément cette image.",
    detail: Annotated[str, Field(pattern=r"^(fast|balanced|precise)$")] = "balanced",
    justification: str = "",
) -> dict[str, Any]:
    """Analyze a local image with Gemma 4 through the local vision service."""
    target = _path(ctx, path)
    raw = target.read_bytes()
    data: dict[str, Any] = {
        "path": str(target),
        "media_type": mimetypes.guess_type(target.name)[0],
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    binary = shutil.which("sips")
    if binary:
        code, stdout, _ = await _command(
            [binary, "-g", "pixelWidth", "-g", "pixelHeight", str(target)], 15
        )
        if code == 0:
            for line in stdout.decode(errors="replace").splitlines():
                key, separator, value = line.strip().partition(":")
                if separator and key in {"pixelWidth", "pixelHeight"}:
                    data[key] = int(value.strip())
    try:
        observation = await LocalVisionService(ctx.deps.events.directory.parent).analyze_path(
            target, question, detail
        )
    except VisionUnavailable as exc:
        return _failure("vision_unavailable", str(exc))
    data["observation"] = observation
    data["detail"] = detail
    return {
        "ok": True,
        "data": data,
        "error": None,
        "metadata": {"engine": "gemma-4-local", "observation_chars": len(observation)},
    }


async def screenshot_capture(
    ctx: RunContext[Any],
    interactive: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Capture the current macOS screen into a session artifact after approval."""
    binary = shutil.which("screencapture")
    if not binary:
        return _failure("precondition", "screencapture is unavailable on this system")
    directory = ctx.deps.events.directory / "artifacts" / str(ctx.deps.session_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"screenshot-{uuid4().hex}.png"
    command = [binary, "-x"]
    if interactive:
        command.append("-i")
    command.append(str(target))
    code, _, stderr = await _command(command, 30)
    if code or not target.exists():
        return _failure("execution", stderr.decode(errors="replace")[:4000] or "capture failed")
    return {
        "ok": True,
        "data": {"path": str(target), "bytes": target.stat().st_size},
        "error": None,
        "metadata": {"media_type": "image/png"},
    }


class PerceptionModule:
    def toolsets(self):
        return [
            FunctionToolset(tools=[pdf_extract, ocr_extract, image_inspect, screenshot_capture])
        ]

    def instructions(self):
        return [
            "Use image_inspect for screenshots and local images when visual evidence "
            "is needed. It delegates to Gemma 4 locally, including when the active "
            "main model is text-only. Use document extraction before inspecting "
            "large documents. Screen capture always requires explicit approval."
        ]

    def capabilities(self):
        return []


module = PerceptionModule()
