"""Per-URL cache of fetch results (JSON files) plus saved originals (PDFs, binary files).

Results are stored as tagged JSON and decoded back into the model's sum types. Anything that
doesn't decode cleanly is treated as a cache miss.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import assert_never

from agent_web.jsonvalue import as_dict, as_list
from agent_web.model import (
    BinaryFile,
    BotCheck,
    Content,
    ConversionFallback,
    CookiesMightHelp,
    FetchResult,
    HiddenText,
    HtmlPage,
    HttpErrorStatus,
    Issue,
    LikelyNeedsJavaScript,
    Meta,
    PdfDocument,
    PdfLayoutWarning,
    PlainText,
    RedditThread,
    ScannedPdf,
)

FORMAT_VERSION = 1


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


class _Bad(Exception):
    """Internal: a cached entry doesn't decode. Never leaves this module."""


def _str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise _Bad(key)
    return value


def _int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Bad(key)
    return value


def _float(data: dict[str, object], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Bad(key)
    return float(value)


def _bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise _Bad(key)
    return value


def _encode_content(content: Content) -> dict[str, object]:
    match content:
        case HtmlPage():
            tag = "html"
        case PdfDocument():
            tag = "pdf"
        case RedditThread():
            tag = "reddit"
        case PlainText():
            tag = "text"
        case BinaryFile():
            tag = "binary"
        case _:
            assert_never(content)
    return {"type": tag, **asdict(content)}


def _encode_issue(issue: Issue) -> dict[str, object]:
    match issue:
        case HttpErrorStatus(status):
            return {"type": "http_error", "status": status}
        case BotCheck(evidence):
            return {"type": "bot_check", "evidence": evidence}
        case LikelyNeedsJavaScript(html_chars, text_chars):
            return {"type": "needs_js", "html_chars": html_chars, "text_chars": text_chars}
        case HiddenText(count):
            return {"type": "hidden_text", "count": count}
        case ScannedPdf():
            return {"type": "scanned_pdf"}
        case PdfLayoutWarning():
            return {"type": "pdf_layout"}
        case CookiesMightHelp(domain):
            return {"type": "cookies_might_help", "domain": domain}
        case ConversionFallback(reason):
            return {"type": "conversion_fallback", "reason": reason}
        case _:
            assert_never(issue)


def encode(result: FetchResult) -> dict[str, object]:
    return {
        "version": FORMAT_VERSION,
        "content": _encode_content(result.content),
        "issues": [_encode_issue(i) for i in result.issues],
    }


def _decode_meta(data: dict[str, object]) -> Meta:
    return Meta(
        url=_str(data, "url"),
        final_url=_str(data, "final_url"),
        status=_int(data, "status"),
        content_type=_str(data, "content_type"),
        cookies_sent=_bool(data, "cookies_sent"),
        fetched_at=_float(data, "fetched_at"),
    )


def _decode_content(data: dict[str, object]) -> Content:
    meta_data = as_dict(data.get("meta"))
    if meta_data is None:
        raise _Bad("meta")
    meta = _decode_meta(meta_data)
    match _str(data, "type"):
        case "html":
            return HtmlPage(meta, title=_str(data, "title"), markdown=_str(data, "markdown"))
        case "pdf":
            return PdfDocument(
                meta,
                markdown=_str(data, "markdown"),
                pages=_int(data, "pages"),
                saved_to=_str(data, "saved_to"),
            )
        case "reddit":
            return RedditThread(
                meta,
                title=_str(data, "title"),
                markdown=_str(data, "markdown"),
                comments=_int(data, "comments"),
                not_loaded=_int(data, "not_loaded"),
            )
        case "text":
            return PlainText(meta, text=_str(data, "text"))
        case "binary":
            return BinaryFile(meta, saved_to=_str(data, "saved_to"), size=_int(data, "size"))
        case other:
            raise _Bad(f"content type {other}")


def _decode_issue(data: dict[str, object]) -> Issue:
    match _str(data, "type"):
        case "http_error":
            return HttpErrorStatus(_int(data, "status"))
        case "bot_check":
            return BotCheck(_str(data, "evidence"))
        case "needs_js":
            return LikelyNeedsJavaScript(_int(data, "html_chars"), _int(data, "text_chars"))
        case "hidden_text":
            return HiddenText(_int(data, "count"))
        case "scanned_pdf":
            return ScannedPdf()
        case "pdf_layout":
            return PdfLayoutWarning()
        case "cookies_might_help":
            return CookiesMightHelp(_str(data, "domain"))
        case "conversion_fallback":
            return ConversionFallback(_str(data, "reason"))
        case other:
            raise _Bad(f"issue type {other}")


def decode(payload: object) -> FetchResult | None:
    data = as_dict(payload)
    if data is None or data.get("version") != FORMAT_VERSION:
        return None
    content_data = as_dict(data.get("content"))
    issue_list = as_list(data.get("issues"))
    if content_data is None or issue_list is None:
        return None
    try:
        issues: list[Issue] = []
        for item in issue_list:
            issue_data = as_dict(item)
            if issue_data is None:
                return None
            issues.append(_decode_issue(issue_data))
        return FetchResult(_decode_content(content_data), tuple(issues))
    except _Bad:
        return None


class PageCache:
    def __init__(
        self,
        directory: Path,
        ttl: float,
        max_bytes: int,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.directory = directory
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._now = now

    @property
    def pages_dir(self) -> Path:
        return self.directory / "pages"

    @property
    def files_dir(self) -> Path:
        return self.directory / "files"

    def page_path(self, url: str) -> Path:
        return self.pages_dir / f"{url_key(url)}.json"

    def load(self, url: str) -> FetchResult | None:
        path = self.page_path(url)
        try:
            if self.ttl <= 0 or self._now() - path.stat().st_mtime > self.ttl:
                return None
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        result = decode(payload)
        if result is None or result.content.meta.url != url:
            return None
        return replace(result, from_cache=True)

    def store(self, url: str, result: FetchResult) -> Path:
        path = self.page_path(url)
        _atomic_write(path, json.dumps(encode(result), ensure_ascii=False).encode("utf-8"))
        self.prune()
        return path

    def save_file(self, url: str, data: bytes, suffix: str) -> Path:
        path = self.files_dir / f"{url_key(url)}{suffix}"
        _atomic_write(path, data)
        return path

    def prune(self) -> None:
        """Delete the oldest entries once the cache is over its size limit."""
        entries: list[tuple[float, int, Path]] = []
        for folder in (self.pages_dir, self.files_dir):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                with contextlib.suppress(OSError):
                    stat = path.stat()
                    entries.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in entries)
        if total <= self.max_bytes:
            return
        for _, size, path in sorted(entries):
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size
            if total <= self.max_bytes * 0.8:
                break


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
