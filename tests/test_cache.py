from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from agent_web.cache import PageCache, decode, encode
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

URL = "https://example.com/page"
META = Meta(URL, URL + "?final", 200, "text/html", True, 1234.5)

CONTENTS: list[Content] = [
    HtmlPage(META, title="T", markdown="# Hello ⟦hidden on page: x⟧"),
    PdfDocument(META, markdown="--- Page 1 ---", pages=1, saved_to="/tmp/x.pdf"),
    RedditThread(META, title="R", markdown="## Post", comments=3, not_loaded=7),
    PlainText(META, text="plain"),
    BinaryFile(META, saved_to="/tmp/x.bin", size=99),
]
ISSUES: list[Issue] = [
    HttpErrorStatus(404),
    BotCheck("captcha"),
    LikelyNeedsJavaScript(100, 1),
    HiddenText(2),
    ScannedPdf(),
    PdfLayoutWarning(),
    CookiesMightHelp("reddit.com"),
    ConversionFallback("why"),
]


def variant_name(content: Content) -> str:
    return type(content).__name__


def valid_content() -> dict[str, object]:
    return cast("dict[str, object]", encode(FetchResult(CONTENTS[0], ()))["content"])


@pytest.mark.parametrize("content", CONTENTS, ids=variant_name)
def test_round_trip_every_variant(content: Content) -> None:
    result = FetchResult(content, tuple(ISSUES))
    assert decode(json.loads(json.dumps(encode(result)))) == result


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"version": 99},
        {"version": 1, "content": {"type": "html"}, "issues": []},
        {"version": 1, "content": {**valid_content(), "type": "video"}, "issues": []},
        {"version": 1, "content": valid_content(), "issues": [{"type": "nope"}]},
        {"version": 1, "content": valid_content(), "issues": ["x"]},
    ],
)
def test_bad_payloads_decode_to_none(payload: object) -> None:
    assert decode(payload) is None


def test_store_and_load(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    result = FetchResult(CONTENTS[0], (HiddenText(1),))
    cache.store(URL, result)
    loaded = cache.load(URL)
    assert loaded is not None
    assert loaded.from_cache
    assert loaded.content == result.content
    assert cache.load("https://example.com/other") is None


def test_variants_are_separate_entries(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    cache.store(URL, FetchResult(CONTENTS[0], ()), "anonymous")
    assert cache.load(URL, "anonymous") is not None
    assert cache.load(URL, "cookies:firefox") is None
    assert cache.load(URL) is None


def test_expiry(tmp_path: Path) -> None:
    clock = [1000.0]
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000, now=lambda: clock[0])
    path = cache.store(URL, FetchResult(CONTENTS[0], ()))
    os.utime(path, (1000.0, 1000.0))
    clock[0] = 1059.0
    assert cache.load(URL) is not None
    clock[0] = 1061.0
    assert cache.load(URL) is None
    assert PageCache(tmp_path, ttl=0, max_bytes=10_000_000).load(URL) is None


def test_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    path = cache.store(URL, FetchResult(CONTENTS[0], ()))
    path.write_text("{not json", encoding="utf-8")
    assert cache.load(URL) is None


def test_prune_removes_oldest(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=600, max_bytes=3000)
    for i in range(5):
        path = cache.save_file(f"https://example.com/{i}", b"z" * 1000, ".bin")
        os.utime(path, (1000.0 + i, 1000.0 + i))
    cache.prune()
    remaining = sorted(p.name for p in cache.files_dir.iterdir())
    total = sum(p.stat().st_size for p in cache.files_dir.iterdir())
    assert total <= 2400
    assert len(remaining) == 2
    newest = cache.save_file("https://example.com/4", b"z", ".bin")
    assert newest.exists()
