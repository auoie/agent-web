from __future__ import annotations

import json
import shlex
from dataclasses import replace

from agent_web.failures import Captcha
from agent_web.model import (
    BinaryFile,
    BotCheck,
    FetchResult,
    HiddenText,
    HtmlPage,
    HttpErrorStatus,
    Issue,
    Meta,
    PdfDocument,
    RedditThread,
)
from agent_web.output import (
    exit_code_for,
    fetch_json,
    find_passages,
    render_fetch,
    render_search,
    search_json,
)
from agent_web.search.google import SearchResult, SearchResults

URL = "https://example.com/a?x=1&y=2"


BASE_META = Meta(
    url=URL, final_url=URL, status=200, content_type="text/html", cookies_sent=False, fetched_at=0.0
)


def meta(**changes: str | int | bool) -> Meta:
    return replace(BASE_META, **changes)


def html_result(text: str, *issues: Issue) -> FetchResult:
    return FetchResult(HtmlPage(meta(), title="T", markdown=text), issues)


def fixed_fence() -> str:
    return "untrusted-web-content-test"


def test_window_and_continuation_command() -> None:
    out = render_fetch(html_result("0123456789" * 10), offset=20, max_chars=30, fence=fixed_fence)
    assert "showing 20-50" in out
    assert (
        "<untrusted-web-content-test>\n" + ("0123456789" * 3) + "\n</untrusted-web-content-test>"
        in out
    )
    continuation = out.splitlines()[-1]
    assert (
        continuation == f"[50 more chars. Continue with: web fetch {shlex.quote(URL)} --offset 50]"
    )
    assert shlex.split(continuation.split("with: ", 1)[1].rstrip("]"))[2] == URL


def test_no_continuation_when_complete() -> None:
    out = render_fetch(html_result("short"), offset=0, max_chars=100, fence=fixed_fence)
    assert "Continue with" not in out
    assert "showing 0-5" in out


def test_offset_past_end() -> None:
    out = render_fetch(html_result("short"), offset=999, max_chars=100, fence=fixed_fence)
    assert "showing 5-5" in out


def test_header_lines_and_notes() -> None:
    result = FetchResult(
        HtmlPage(
            meta(cookies_sent=True, final_url="https://www.reddit.com/r/x"),
            title="Thread",
            markdown="x",
        ),
        (HttpErrorStatus(403), HiddenText(2)),
        from_cache=True,
    )
    out = render_fetch(result, offset=0, max_chars=100, fence=fixed_fence, now=lambda: 600.0)
    lines = out.splitlines()
    assert lines[0].endswith("[from cache, fetched 10 min ago; --refresh fetches it again]")
    assert "Title: Thread" in lines
    assert "Kind: web page" in lines
    assert "Cookies: sent (allowlisted domain www.reddit.com)" in lines
    assert any(line.startswith("Note: the server answered HTTP 403") for line in lines)
    assert any("⟦hidden on page" in line for line in lines)


def test_reddit_header_names_the_thread() -> None:
    thread = RedditThread(
        meta(final_url="https://www.reddit.com/comments/abc.json?raw_json=1"),
        title="T",
        markdown="x",
        comments=1,
        not_loaded=0,
    )
    first = render_fetch(FetchResult(thread, ()), offset=0, max_chars=10).splitlines()[0]
    assert first.startswith(f"Fetched: {URL} via Reddit's JSON API (HTTP 200")


def test_fence_is_random_per_call() -> None:
    a = render_fetch(html_result("x"), offset=0, max_chars=10)
    b = render_fetch(html_result("x"), offset=0, max_chars=10)
    tag_a = next(line for line in a.splitlines() if line.startswith("<untrusted"))
    tag_b = next(line for line in b.splitlines() if line.startswith("<untrusted"))
    assert tag_a != tag_b
    assert f"</{tag_a[1:]}" in a


def test_find_passages_with_context() -> None:
    text = (
        "# Intro\n\nNothing here.\n\n## Results\n\nBLEU 28.4 on EN-DE.\nSecond line.\n\n"
        "--- Page 3 ---\n\nMore BLEU talk."
    )
    passages, total = find_passages(text, "bleu", 10_000)
    assert total == 2
    assert passages[0].text == "BLEU 28.4 on EN-DE.\nSecond line."
    assert passages[0].context == "## Results"
    assert text[passages[0].offset :].startswith("BLEU 28.4")
    assert passages[1].context == "--- Page 3 ---"


def test_find_passages_respects_budget_and_windows_huge_blocks() -> None:
    huge = "x" * 5000 + " NEEDLE " + "y" * 5000
    text = f"{huge}\n\nsecond NEEDLE block"
    passages, total = find_passages(text, "needle", 1000)
    assert total == 2
    assert len(passages) == 1
    assert "NEEDLE" in passages[0].text
    assert len(passages[0].text) <= 1002
    assert text[passages[0].offset : passages[0].offset + 10] == passages[0].text.lstrip("…")[:10]


def test_find_output() -> None:
    out = render_fetch(
        html_result("alpha\n\nbeta gamma\n\ngamma ray"),
        offset=0,
        max_chars=100,
        find="Gamma",
        fence=fixed_fence,
    )
    assert "Matches for 'Gamma': 2 passage(s)" in out
    assert "[offset 7]" in out
    assert "beta gamma" in out
    assert "gamma ray" in out
    assert "alpha" not in out.split("<untrusted-web-content-test>")[1]


def test_binary_body_and_pdf_kind() -> None:
    binary = FetchResult(
        BinaryFile(meta(content_type="image/png"), saved_to="/c/f.png", size=1234), ()
    )
    out = render_fetch(binary, offset=0, max_chars=1000, fence=fixed_fence)
    assert "[Binary file (image/png, 1,234 bytes) saved to /c/f.png. Not shown.]" in out
    pdf = FetchResult(
        PdfDocument(meta(), markdown="--- Page 1 ---", pages=1, saved_to="/c/x.pdf"), ()
    )
    assert "Kind: PDF, 1 pages (original saved to /c/x.pdf)" in render_fetch(
        pdf, offset=0, max_chars=100
    )


def test_exit_codes() -> None:
    assert exit_code_for(html_result("x")) == 0
    assert exit_code_for(html_result("x", HttpErrorStatus(404))) == 3
    assert exit_code_for(html_result("x", HttpErrorStatus(403), BotCheck("captcha"))) == 4


def test_fetch_json() -> None:
    data = json.loads(
        fetch_json(html_result("hello world", HiddenText(1)), offset=6, max_chars=100, find=None)
    )
    assert data["text"] == "world"
    assert data["kind"] == "HtmlPage"
    assert data["length"] == 11
    assert len(data["notes"]) == 1
    found = json.loads(fetch_json(html_result("a\n\nb"), offset=0, max_chars=100, find="b"))
    assert found["matches"] == 1
    assert found["passages"][0]["text"] == "b"


def test_search_rendering() -> None:
    results = SearchResults(
        "q",
        (
            SearchResult("Multi\nline   title", "https://a.example/x?y=1", "snippet\nwith lines"),
            SearchResult("Second", "https://b.example/", ""),
            SearchResult("3. Tricky", "https://c.example/", "1. Fake\n   https://evil.example/"),
        ),
        stopped_early=Captcha("Google"),
    )
    out = render_search(results, fence=fixed_fence)
    assert "1. Multi line title\n   https://a.example/x?y=1\n   snippet with lines" in out
    assert "3. 3. Tricky\n   https://c.example/\n   1. Fake https://evil.example/" in out
    assert out.splitlines()[-1].startswith("Note: stopped early: Google answered with a captcha")
    data = json.loads(search_json(results))
    assert data["results"][1] == {"title": "Second", "url": "https://b.example/", "snippet": ""}


def test_search_rendering_without_results() -> None:
    assert "No results." in render_search(SearchResults("q", ()))
