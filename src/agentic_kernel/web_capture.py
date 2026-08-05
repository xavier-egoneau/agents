"""Capture d'une page web en markdown, via le binaire `ketch`.

`ketch` extrait déjà le contenu lisible d'une page et recharge les rendus
JavaScript en Chrome headless. Le module d'outils `web` s'en sert pour
`web_scrape`; l'ingestion en a besoin pour la même chose. Cette fonction est
donc partagée plutôt que dupliquée des deux côtés.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from .converters import ConversionError, Converted
from .managed_tools import discovered_executable

MAX_OUTPUT_BYTES = 2_000_000

# Clés sous lesquelles ketch peut ranger le corps et le titre. On les cherche
# toutes plutôt que d'en figer une : la sortie est un contrat externe, et se
# tromper de clé produirait un document vide sans rien signaler.
_BODY_KEYS = ("markdown", "content", "text", "body", "excerpt")
_TITLE_KEYS = ("title", "name", "heading")


def resolve_binary() -> str | None:
    return discovered_executable("ketch", "AMK_KETCH_BIN")


def is_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _extract(payload: object) -> tuple[str, str]:
    """Retrouve titre et corps dans une sortie dont la forme n'est pas garantie."""
    if isinstance(payload, str):
        return "", payload.strip()
    if isinstance(payload, list):
        for item in payload:
            title, body = _extract(item)
            if body:
                return title, body
        return "", ""
    if isinstance(payload, dict):
        title = next(
            (payload[key] for key in _TITLE_KEYS if isinstance(payload.get(key), str)),
            "",
        )
        for key in _BODY_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return title, value.strip()
        for value in payload.values():
            if isinstance(value, dict | list):
                nested_title, body = _extract(value)
                if body:
                    return title or nested_title, body
        return title, ""
    return "", ""


async def scrape_to_markdown(url: str, *, max_chars: int = 20_000) -> Converted:
    if not is_url(url):
        raise ConversionError(f"URL invalide : {url}")
    binary = resolve_binary()
    if binary is None:
        raise ConversionError(
            "ketch est absent du PATH. Binaires prébuilts : "
            "https://github.com/1broseidon/ketch/releases"
        )
    process = await asyncio.create_subprocess_exec(
        binary,
        "scrape",
        url,
        "--max-chars",
        str(max_chars),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError:
        process.terminate()
        await process.wait()
        raise ConversionError(f"délai dépassé en récupérant {url}") from None
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[:500].strip()
        raise ConversionError(detail or f"ketch a échoué sur {url}")

    text = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace").strip()
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    title, body = _extract(payload)
    if not body:
        raise ConversionError(f"aucun contenu exploitable renvoyé pour {url}")
    return Converted(title=title.strip(), body=body, source_type="web")
