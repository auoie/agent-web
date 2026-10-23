from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from agent_web.cache import FORMAT_VERSION, PageCache, decode, encode, file_stem
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
    NearlyEmpty,
    OutsideMain,
    PdfDocument,
    PdfLayoutWarning,
    PlainText,
    RedditThread,
    SavedFiles,
    ScannedPdf,
)

URL = "https://example.com/page"
META = Meta(URL, URL + "?final", 200, "text/html", True, 1234.5)

CONTENTS: list[Content] = [
    HtmlPage(META, title="T", markdown="# Hello ⟦hidden on page: x⟧"),
    PdfDocument(META, markdown="--- Page 1 ---", pages=1),
    RedditThread(META, title="R", markdown="## Post", comments=3, not_loaded=7),
    PlainText(META, text="plain"),
    BinaryFile(META, size=99),
]
ISSUES: list[Issue] = [
    HttpErrorStatus(404),
    BotCheck("captcha"),
    LikelyNeedsJavaScript(100, 1),
    NearlyEmpty(3),
    OutsideMain(500),
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


def store(cache: PageCache, variant: str = "", issues: tuple[Issue, ...] = ()) -> Path:
    """Store a page the way fetch does: its files first, then the entry."""
    files = cache.save_files(URL, variant, b"<h1>Hello</h1>", ".html", "# Hello")
    return cache.store(URL, FetchResult(CONTENTS[0], issues, files=files), variant)


@pytest.mark.parametrize("content", CONTENTS, ids=variant_name)
@pytest.mark.parametrize(
    "files", [None, SavedFiles("/c/files/x.html", "/c/text/x.md"), SavedFiles("/c/x.png", None)]
)
def test_round_trip_every_variant(content: Content, files: SavedFiles | None) -> None:
    result = FetchResult(content, tuple(ISSUES), files=files)
    assert decode(json.loads(json.dumps(encode(result)))) == result


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"version": 99},
        {"version": FORMAT_VERSION, "content": {"type": "html"}, "issues": []},
        {"version": FORMAT_VERSION, "content": {**valid_content(), "type": "video"}, "issues": []},
        {"version": FORMAT_VERSION, "content": valid_content(), "issues": [{"type": "nope"}]},
        {"version": FORMAT_VERSION, "content": valid_content(), "issues": ["x"]},
        {"version": FORMAT_VERSION, "content": valid_content(), "issues": [], "files": "x"},
        {"version": FORMAT_VERSION, "content": valid_content(), "issues": [], "files": {}},
    ],
)
def test_bad_payloads_decode_to_none(payload: object) -> None:
    assert decode(payload) is None


def test_entries_from_an_older_format_are_misses() -> None:
    payload = encode(FetchResult(CONTENTS[0], ()))
    assert decode(payload) is not None
    assert decode({**payload, "version": FORMAT_VERSION - 1}) is None


def test_store_and_load(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    store(cache, issues=(HiddenText(1),))
    loaded = cache.load(URL)
    assert loaded is not None
    assert loaded.from_cache
    assert loaded.content == CONTENTS[0]
    assert loaded.issues == (HiddenText(1),)
    assert loaded.files is not None
    assert Path(loaded.files.original).read_bytes() == b"<h1>Hello</h1>"
    assert loaded.files.text is not None
    assert Path(loaded.files.text).read_text(encoding="utf-8") == "# Hello"
    assert cache.load("https://example.com/other") is None


@pytest.mark.parametrize("removed", ["original", "text"])
def test_entry_whose_files_were_removed_is_a_miss(tmp_path: Path, removed: str) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    store(cache)
    loaded = cache.load(URL)
    assert loaded is not None
    assert loaded.files is not None
    Path(getattr(loaded.files, removed)).unlink()
    assert cache.load(URL) is None


def test_entry_without_files_is_a_miss(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    cache.store(URL, FetchResult(CONTENTS[0], ()))
    assert cache.load(URL) is None


def test_saved_files_are_named_after_the_site(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    files = cache.save_files(URL, "anonymous", b"%PDF", ".pdf", "text")
    assert files.original == str(cache.files_dir / f"{file_stem(URL, 'anonymous')}.pdf")
    assert files.text == str(cache.text_dir / f"{file_stem(URL, 'anonymous')}.md")
    assert file_stem(URL, "anonymous").startswith("example.com-")
    assert file_stem(URL, "anonymous") != file_stem(URL, "cookies:firefox")
    assert file_stem("http://[::1]/x").startswith("__1-")
    binary = cache.save_files("https://example.com/i.png", "", b"png", ".png", None)
    assert binary.text is None
    assert Path(binary.original).read_bytes() == b"png"


def test_variants_are_separate_entries(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    store(cache, "anonymous")
    assert cache.load(URL, "anonymous") is not None
    assert cache.load(URL, "cookies:firefox") is None
    assert cache.load(URL) is None


def test_expiry(tmp_path: Path) -> None:
    clock = [1000.0]
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000, now=lambda: clock[0])
    path = store(cache)
    os.utime(path, (1000.0, 1000.0))
    clock[0] = 1059.0
    assert cache.load(URL) is not None
    clock[0] = 1061.0
    assert cache.load(URL) is None
    assert PageCache(tmp_path, ttl=0, max_bytes=10_000_000).load(URL) is None


def test_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=60, max_bytes=10_000_000)
    path = store(cache)
    path.write_text("{not json", encoding="utf-8")
    assert cache.load(URL) is None


def test_prune_removes_oldest_across_folders(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=600, max_bytes=3000)
    folders = [cache.files_dir, cache.text_dir, cache.pages_dir, cache.files_dir, cache.text_dir]
    for i, folder in enumerate(folders):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / str(i)
        path.write_bytes(b"z" * 1000)
        os.utime(path, (1000.0 + i, 1000.0 + i))
    cache.prune()
    remaining = sorted(p.name for folder in set(folders) for p in folder.iterdir())
    assert remaining == ["3", "4"]
    newest = cache.save_files("https://example.com/new", "", b"z", ".bin", None)
    assert Path(newest.original).exists()


def test_prune_keeps_the_files_a_fetch_just_saved(tmp_path: Path) -> None:
    cache = PageCache(tmp_path, ttl=600, max_bytes=1000)
    files = cache.save_files("https://example.com/big", "", b"z" * 5000, ".bin", None)
    cache.prune(keep=[files.original])
    assert Path(files.original).exists()  # bigger than the whole cache, but just printed
