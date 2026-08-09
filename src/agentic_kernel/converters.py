"""Conversion de documents déposés vers du markdown lisible.

Les URL ne passent pas par ici : `web_scrape` extrait déjà le contenu lisible
d'une page, recharge les rendus JavaScript en Chrome headless et est éprouvé.
Ce module ne traite que les fichiers locaux, où cette capacité n'existe pas.

Le HTML est converti avec `html.parser` de la bibliothèque standard. Une
dépendance dédiée ferait mieux sur du balisage hostile, mais le chemin principal
— une URL via `web_scrape` — est déjà couvert : ajouter une dépendance pour le
cas secondaire serait mal payé.
"""

from __future__ import annotations

import asyncio
import html
import re
import shutil
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

MAX_BYTES = 5_000_000

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
PDF_SUFFIXES = {".pdf"}

SUPPORTED = TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES

# Contenu non textuel : conservé tel quel dans le flux, il produirait du bruit
# indexé sans valeur de lecture. `head` n'y figure pas — il contient `<title>`,
# la meilleure source de titre du document; ses autres balises n'ont pas de
# contenu textuel, et `script`/`style` sont déjà filtrés ici.
_DROPPED = {"script", "style", "noscript", "svg", "canvas"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "br", "tr", "table"}
_HEADINGS = {f"h{level}": "#" * level for level in range(1, 7)}


class ConversionError(Exception):
    """Le document ne peut pas être converti; le message est destiné à l'agent."""


@dataclass(frozen=True)
class Converted:
    title: str
    body: str
    source_type: str


class _MarkdownExtractor(HTMLParser):
    """Extrait titre et texte structuré d'un document HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._list_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._chunks.append(f"\n\n{_HEADINGS[tag]} ")
        elif tag in {"ul", "ol"}:
            self._list_stack.append(tag)
            self._chunks.append("\n")
        elif tag == "li":
            marker = "1." if self._list_stack and self._list_stack[-1] == "ol" else "-"
            self._chunks.append(f"\n{marker} ")
        elif tag in {"pre", "blockquote"} | _BLOCK:
            self._chunks.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in {"ul", "ol"} and self._list_stack:
            self._list_stack.pop()
            self._chunks.append("\n")
        elif tag in _HEADINGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        if data.strip():
            self._chunks.append(data)

    def markdown(self) -> str:
        text = "".join(self._chunks)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_markdown(source: str) -> Converted:
    parser = _MarkdownExtractor()
    parser.feed(html.unescape(source) if "&" not in source else source)
    body = parser.markdown()
    if not body:
        raise ConversionError("aucun texte exploitable dans ce document HTML")
    return Converted(title=parser.title.strip(), body=body, source_type="web")


async def pdf_to_markdown(path: Path) -> Converted:
    """Extrait le texte d'un PDF via `pdftotext`, comme le module perception."""
    binary = shutil.which("pdftotext")
    if binary is None:
        raise ConversionError(
            "pdftotext est absent : installer poppler, ou convertir le PDF en texte "
            "avant de le déposer"
        )
    process = await asyncio.create_subprocess_exec(
        binary,
        "-layout",
        str(path),
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError:
        process.terminate()
        await process.wait()
        raise ConversionError("extraction PDF interrompue au bout de 120 s") from None
    if process.returncode:
        raise ConversionError(stderr.decode(errors="replace")[:500] or "extraction PDF échouée")
    body = re.sub(r"\n{3,}", "\n\n", stdout.decode(errors="replace")).strip()
    if not body:
        raise ConversionError(
            "aucun texte extrait : ce PDF est probablement une image scannée, "
            "utiliser ocr_extract"
        )
    return Converted(title=path.stem, body=body, source_type="pdf")


def text_to_markdown(path: Path, raw: str) -> Converted:
    """Conserve le markdown tel quel, en retenant son premier titre s'il existe."""
    body = raw.strip()
    heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return Converted(
        title=(heading.group(1).strip() if heading else path.stem),
        body=body,
        source_type="text",
    )


async def convert_file(path: Path) -> Converted:
    """Convertit un fichier déposé, ou explique pourquoi c'est impossible."""
    if not path.is_file():
        raise ConversionError(f"fichier introuvable : {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ConversionError(
            f"format non pris en charge : {suffix or 'sans extension'}. "
            f"Formats acceptés : {', '.join(sorted(SUPPORTED))}"
        )
    if path.stat().st_size > MAX_BYTES:
        raise ConversionError(f"document trop volumineux (limite {MAX_BYTES // 1_000_000} Mo)")
    if suffix in PDF_SUFFIXES:
        return await pdf_to_markdown(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConversionError("document illisible en UTF-8") from exc
    if suffix in HTML_SUFFIXES:
        converted = html_to_markdown(raw)
        return Converted(
            title=converted.title or path.stem,
            body=converted.body,
            source_type=converted.source_type,
        )
    return text_to_markdown(path, raw)
