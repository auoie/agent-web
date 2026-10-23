"""PDF to markdown with pymupdf (no OCR), one `--- Page N ---` marker per page."""

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class PdfConversion:
    markdown: str
    pages: int
    text_chars: int


@dataclass(frozen=True)
class PdfUnreadable:
    reason: str


@contextlib.contextmanager
def _quiet_stdout() -> Generator[None]:
    """MuPDF writes parser messages straight to file descriptor 1, and pymupdf4llm prints
    warnings; keep both out of our output."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        with Path(os.devnull).open("w") as devnull:
            os.dup2(devnull.fileno(), 1)
            yield
    finally:
        # Printed text still in Python's buffer (a pipe's is flushed only when full) goes to
        # /dev/null too, not to the real stdout after it's restored.
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _page_text(chunk: Mapping[str, object]) -> str:
    text = chunk.get("text")
    return text.strip() if isinstance(text, str) else ""


def convert_pdf(data: bytes) -> PdfConversion | PdfUnreadable:
    import pymupdf

    llm = pymupdf.pymupdf4llm  # pyright: ignore[reportPrivateImportUsage]
    try:
        with _quiet_stdout():
            document = pymupdf.open(stream=data, filetype="pdf")
            try:
                # pymupdf4llm.to_markdown is only partly annotated; treat its result as unknown
                # data and validate it below.
                chunks = cast(
                    "object",
                    llm.to_markdown(  # pyright: ignore[reportUnknownMemberType]
                        document, page_chunks=True, use_ocr=False, show_progress=False
                    ),
                )
            finally:
                document.close()
    except Exception as e:  # untrusted input parsed by a C library: any failure means unreadable
        return PdfUnreadable(f"{type(e).__name__}: {e}"[:300])
    if isinstance(chunks, str):
        pages = [chunks.strip()]
    elif isinstance(chunks, list):
        items = cast("list[object]", chunks)
        pages = [
            _page_text(cast("Mapping[str, object]", c)) if isinstance(c, dict) else ""
            for c in items
        ]
    else:
        return PdfUnreadable(f"unexpected converter output: {type(chunks).__name__}")
    markdown = "\n\n".join(f"--- Page {i} ---\n\n{text}" for i, text in enumerate(pages, 1))
    text_chars = len(re.sub(r"\s+", "", "".join(pages)))
    return PdfConversion(markdown=markdown, pages=len(pages), text_chars=text_chars)
