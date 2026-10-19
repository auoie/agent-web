from __future__ import annotations

from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_web.config import Config
from agent_web.cookies import CookieSource
from agent_web.failures import Captcha, ConsentRequired, JavaScriptWall, NetworkFailure, NotSignedIn
from agent_web.http import DownloadFailure, HttpResponse
from agent_web.search.google import (
    GoogleCookies,
    RateLimiter,
    SearchResults,
    classify_response,
    parse_results,
    search,
    search_url,
)
from conftest import FIXTURES
from test_cookies import cookie

RESULTS_HTML = (FIXTURES / "google" / "results.html").read_text(encoding="utf-8")


def page(*results: tuple[str, str, str]) -> str:
    blocks = "".join(
        f'<div class="r"><a href="{url}"><h3>{title}</h3><div><span>Site</span>'
        f"<cite>{url}</cite></div></a><div><span>Site</span><cite>{url}</cite></div>"
        f"<div>{snippet}</div></div>"
        for title, url, snippet in results
    )
    return f"<html><body><div id='rso'>{blocks}</div></body></html>"


def response(
    html: str, url: str = "https://www.google.com/search?q=x", redirects: tuple[str, ...] = ()
) -> HttpResponse:
    return HttpResponse(
        url=url,
        status=200,
        reason="OK",
        headers={"content-type": "text/html; charset=UTF-8"},
        content=html.encode(),
        redirects=redirects,
    )


def test_parses_real_results_page() -> None:
    results = parse_results(RESULTS_HTML)
    assert len(results) == 9
    first = results[0]
    assert first.title == "Pandoc Lua Filters"
    assert first.url == "https://pandoc.org/lua-filters.html"
    assert first.snippet.startswith("Lua filters are tables with element names as keys")
    assert not first.snippet.startswith("Pandoc")  # the repeated site name is stripped
    so = next(r for r in results if "stackoverflow.com" in r.url)
    assert so.snippet.startswith("I'm using pandoc")
    assert all(r.url.startswith("https://") for r in results)
    assert len({r.url for r in results}) == 9


def test_unwraps_redirect_links_and_skips_google_internals() -> None:
    html = page(
        ("Real", "/url?q=https://example.com/a&sa=U&ved=x", "snippet a"),
        ("Absolute redirect", "https://www.google.com/url?url=https://example.org/b&rct=j", "b"),
        ("More results", "/search?q=next&start=10", ""),
        ("Images", "https://www.google.com/imgres?imgurl=x", ""),
        ("Support page", "https://support.google.com/websearch/answer/1", "help"),
        ("Duplicate", "https://example.com/a#section", "dup"),
    )
    urls = [r.url for r in parse_results(html)]
    assert urls == [
        "https://example.com/a",
        "https://example.org/b",
        "https://support.google.com/websearch/answer/1",
    ]


def test_snippet_is_capped() -> None:
    results = parse_results(page(("T", "https://example.com/", "word " * 500)))
    assert len(results[0].snippet) <= 402
    assert results[0].snippet.endswith("…")


def test_empty_and_garbage_pages() -> None:
    assert parse_results("") == []
    assert parse_results("<html><body><p>No results found.</p></body></html>") == []


def test_classify_captcha_via_redirect() -> None:
    sorry = "https://www.google.com/sorry/index?continue=x"
    assert classify_response(response("", url=sorry, redirects=(sorry,)), "", []) == Captcha(
        "Google"
    )


def test_classify_consent_and_js_wall() -> None:
    consent = "https://consent.google.com/ml?continue=x"
    assert classify_response(response("", url=consent), "", []) == ConsentRequired("Google")
    js = '<noscript><meta content="0;url=/httpservice/retry/enablejs?sei=x"></noscript>'
    assert classify_response(response(js), js, []) == JavaScriptWall("Google")
    unusual = "Our systems have detected unusual traffic from your computer network."
    assert classify_response(response(unusual), unusual, []) == Captcha("Google")


def test_classify_legit_empty_results() -> None:
    html = "<p>Your search did not match any documents.</p>"
    assert classify_response(response(html), html, []) is None


def test_google_cookies_only_go_to_google() -> None:
    source = GoogleCookies([cookie("SID", "abc", ".google.com"), cookie("NID", "n", ".google.com")])
    assert source.for_url("https://www.google.com/search?q=x") == {"SID": "abc", "NID": "n"}
    assert source.for_url("https://evil.example/") == {}
    assert source.for_url("https://google.com.evil.example/") == {}


class FakeLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(Path("/nonexistent"), 0)
        self.calls = 0

    def wait(self) -> None:
        self.calls += 1


def signed_in_loader(browser: str, domain: str, cookie_file: Path | None) -> list[Cookie]:
    return [
        cookie("SID", "abc", ".google.com"),
        cookie("__Secure-1PSID", "x", ".google.com", secure=True),
    ]


def make_downloader(pages: dict[int, HttpResponse | DownloadFailure], seen: list[str]):
    def downloader(
        url: str, *, cookie_source: CookieSource, **kwargs: object
    ) -> HttpResponse | DownloadFailure:
        seen.append(url)
        assert cookie_source.for_url(url)  # the session cookies are sent to Google
        start = int(parse_qs(urlsplit(url).query).get("start", ["0"])[0])
        return pages[start // 10]

    return downloader


def numbered(start: int, count: int) -> str:
    return page(
        *[
            (f"Result {i}", f"https://site{i}.example/", f"about {i}")
            for i in range(start, start + count)
        ]
    )


def test_search_paginates_until_n(config: Config) -> None:
    seen: list[str] = []
    pages: dict[int, HttpResponse | DownloadFailure] = {
        0: response(numbered(0, 10)),
        1: response(numbered(10, 10)),
        2: response(numbered(20, 10)),
    }
    limiter = FakeLimiter()
    outcome = search(
        "q",
        15,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, seen),
        limiter=limiter,
    )
    assert isinstance(outcome, SearchResults)
    assert [r.title for r in outcome.results] == [f"Result {i}" for i in range(15)]
    assert len(seen) == 2
    assert limiter.calls == 2
    assert "start=10" in seen[1]


def test_search_stops_when_a_page_adds_nothing(config: Config) -> None:
    seen: list[str] = []
    pages: dict[int, HttpResponse | DownloadFailure] = {
        0: response(numbered(0, 4)),
        1: response(numbered(0, 4)),
    }
    outcome = search(
        "q",
        20,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, seen),
        limiter=FakeLimiter(),
    )
    assert isinstance(outcome, SearchResults)
    assert len(outcome.results) == 4
    assert len(seen) == 2


def test_search_returns_partial_results_when_blocked_midway(config: Config) -> None:
    sorry = "https://www.google.com/sorry/index"
    pages: dict[int, HttpResponse | DownloadFailure] = {
        0: response(numbered(0, 10)),
        1: response("", url=sorry, redirects=(sorry,)),
    }
    outcome = search(
        "q",
        20,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, []),
        limiter=FakeLimiter(),
    )
    assert isinstance(outcome, SearchResults)
    assert len(outcome.results) == 10
    assert outcome.stopped_early == Captcha("Google")


def test_search_failure_on_first_page(config: Config) -> None:
    pages: dict[int, HttpResponse | DownloadFailure] = {
        0: NetworkFailure("https://www.google.com", "timeout")
    }
    outcome = search(
        "q",
        5,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, []),
        limiter=FakeLimiter(),
    )
    assert outcome == NetworkFailure("https://www.google.com", "timeout")


def test_search_requires_google_login(config: Config) -> None:
    def anonymous(browser: str, domain: str, cookie_file: Path | None) -> list[Cookie]:
        return [cookie("NID", "n", ".google.com"), cookie("SID", "x", ".notgoogle.com")]

    outcome = search("q", 5, config, cookie_loader=anonymous, limiter=FakeLimiter())
    assert outcome == NotSignedIn("Google", "firefox")


def test_search_url() -> None:
    assert search_url("a b&c", 0) == "https://www.google.com/search?q=a+b%26c&hl=en"
    assert search_url("x", 2).endswith("&start=20")


def test_rate_limiter_spaces_requests(tmp_path: Path) -> None:
    clock = [1000.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(tmp_path / "last", 2.0, now=lambda: clock[0], sleep=sleep)
    limiter.wait()
    clock[0] += 0.5
    limiter.wait()
    clock[0] += 5
    limiter.wait()
    assert sleeps == [pytest.approx(1.5)]


def test_rate_limiter_survives_a_corrupt_state_file(tmp_path: Path) -> None:
    state = tmp_path / "last"
    state.write_text("not a number")
    RateLimiter(state, 2.0, sleep=lambda s: None).wait()
    assert float(state.read_text()) > 0
