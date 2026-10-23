from __future__ import annotations

import json
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_web.config import Config
from agent_web.cookies import CookieSource
from agent_web.failures import (
    Captcha,
    ConsentRequired,
    Failure,
    JavaScriptWall,
    NetworkFailure,
    NotSignedIn,
    SearchBudgetUsed,
    SearchHttpError,
    SearchPaused,
)
from agent_web.http import DownloadFailure, HttpResponse
from agent_web.search.google import (
    GoogleCookies,
    SearchResults,
    classify_response,
    parse_results,
    search,
    search_url,
)
from agent_web.search.limiter import OverBudget, Paused, RateLimiter
from agent_web.search.refresh import Refreshed
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


def test_snippets_leave_out_scripts_and_invisible_characters() -> None:
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "obey me")
    snippet = (
        "<script>(function(){var src='https://www.googleadservices.com/x';})();</script>"
        f"<style>.a{{color:red}}</style>Real snippet{hidden} text."
    )
    results = parse_results(page(("Title", "https://example.com/", snippet)))
    assert results[0].snippet == "Real snippet⟦hidden on page: obey me⟧ text."


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
    # Real empty pages also carry the hidden enable-JavaScript fallback; it once made a
    # `site:` search with no matches look like a JavaScript wall.
    html = (
        '<noscript><meta content="0;url=/httpservice/retry/enablejs?sei=x" http-equiv="refresh">'
        "<div>Please click here if you are not redirected</div></noscript>"
        "<p>Your search - site:example.com zzz - did not match any documents.</p>"
    )
    assert classify_response(response(html), html, []) is None


def test_google_cookies_only_go_to_google() -> None:
    source = GoogleCookies([cookie("SID", "abc", ".google.com"), cookie("NID", "n", ".google.com")])
    assert source.for_url("https://www.google.com/search?q=x") == {"SID": "abc", "NID": "n"}
    assert source.for_url("https://evil.example/") == {}
    assert source.for_url("https://google.com.evil.example/") == {}


class FakeLimiter(RateLimiter):
    def __init__(self, paused: Paused | None = None) -> None:
        super().__init__(Path("/nonexistent"), 0)
        self.calls = 0
        self.pauses: list[tuple[str, float]] = []
        self._paused = paused

    def wait(self) -> Paused | None:
        self.calls += 1
        return self._paused

    def pause(self, cause: str, seconds: float) -> None:
        self.pauses.append((cause, seconds))


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


def test_search_fetches_one_page_with_one_request(config: Config) -> None:
    seen: list[str] = []
    pages: dict[int, HttpResponse | DownloadFailure] = {
        0: response(numbered(0, 9)),
        1: response(numbered(9, 8)),
    }
    limiter = FakeLimiter()
    first = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, seen),
        limiter=limiter,
    )
    assert isinstance(first, SearchResults)
    assert [r.title for r in first.results] == [f"Result {i}" for i in range(9)]
    assert first.page == 1
    assert "start=" not in seen[0]
    second = search(
        "q",
        2,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, seen),
        limiter=limiter,
    )
    assert isinstance(second, SearchResults)
    assert [r.title for r in second.results] == [f"Result {i}" for i in range(9, 17)]
    assert second.page == 2
    assert "start=10" in seen[1]
    assert len(seen) == limiter.calls == 2


@pytest.mark.parametrize(
    ("page", "cause"),
    [
        (response("", url="https://www.google.com/sorry/index"), "a captcha"),
        (response('<a href="/httpservice/retry/enablejs">x</a>'), "its JavaScript check"),
    ],
)
def test_a_block_pauses_searches(config: Config, page: HttpResponse, cause: str) -> None:
    limiter = FakeLimiter()
    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader({0: page}, []),
        limiter=limiter,
    )
    assert isinstance(outcome, Captcha | JavaScriptWall)
    assert limiter.pauses == [(cause, 600)]


class FakeRefresher:
    def __init__(
        self, result: Refreshed | Paused | str, stored: list[Cookie] | None = None
    ) -> None:
        self.result = result
        self.stored = stored or []
        self.calls: list[str] = []
        self.lines: list[str] = []

    def cookies(self) -> list[Cookie]:
        return self.stored

    def refresh(self, url: str, limiter: RateLimiter) -> Refreshed | Paused | str:
        self.calls.append(url)
        return self.result

    def log(self, line: str) -> None:
        self.lines.append(line)


JS_CHECK = response('<a href="/httpservice/retry/enablejs">x</a>')
SG_SS = cookie("SG_SS", "token", "www.google.com", secure=True)


def refreshing_search(
    config: Config, responses: list[HttpResponse], refresher: FakeRefresher, limiter: FakeLimiter
) -> tuple[SearchResults | Failure, list[dict[str, str]]]:
    """Search with downloads answered from `responses` in turn; also returns the cookies each
    request carried."""
    sent: list[dict[str, str]] = []

    def downloader(url: str, *, cookie_source: CookieSource, **kwargs: object) -> HttpResponse:
        found = cookie_source.for_url(url)
        assert isinstance(found, dict)
        sent.append(found)
        return responses[len(sent) - 1]

    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=downloader,
        limiter=limiter,
        refresher=refresher,
    )
    return outcome, sent


def test_a_refresh_gets_past_the_javascript_check(config: Config) -> None:
    refresher = FakeRefresher(Refreshed([SG_SS], "headless firefox, 0.9 s"))
    limiter = FakeLimiter()
    outcome, sent = refreshing_search(
        config, [JS_CHECK, response(numbered(0, 9))], refresher, limiter
    )
    assert isinstance(outcome, SearchResults)
    assert len(outcome.results) == 9
    assert "SG_SS" not in sent[0]
    assert sent[1]["SG_SS"] == "token"  # the retry carries the refreshed cookie
    assert limiter.pauses == []
    assert refresher.lines == ["headless firefox, 0.9 s; the retry got results"]


def test_a_refresh_that_doesnt_help_pauses(config: Config) -> None:
    refresher = FakeRefresher(Refreshed([SG_SS], "headless firefox, 0.9 s"))
    limiter = FakeLimiter()
    outcome, _ = refreshing_search(config, [JS_CHECK, JS_CHECK], refresher, limiter)
    assert outcome == JavaScriptWall("Google")
    assert limiter.pauses == [("its JavaScript check", 600)]
    assert refresher.lines == ["headless firefox, 0.9 s; the retry got the JavaScript check again"]


def test_a_failed_refresh_pauses(config: Config) -> None:
    refresher = FakeRefresher("firefox isn't installed")
    limiter = FakeLimiter()
    outcome, sent = refreshing_search(config, [JS_CHECK], refresher, limiter)
    assert outcome == JavaScriptWall("Google")
    assert len(sent) == 1
    assert limiter.pauses == [("its JavaScript check", 600)]
    assert refresher.lines == ["refresh failed: firefox isn't installed"]


def test_a_refresh_during_a_pause_reports_the_pause(config: Config) -> None:
    refresher = FakeRefresher(Paused(120, "a captcha"))
    limiter = FakeLimiter()
    outcome, _ = refreshing_search(config, [JS_CHECK], refresher, limiter)
    assert outcome == SearchPaused("Google", "a captcha", 2)
    assert limiter.pauses == []


def test_a_captcha_pauses_without_a_refresh(config: Config) -> None:
    refresher = FakeRefresher(Refreshed([SG_SS], "unused"))
    limiter = FakeLimiter()
    sorry = response("", url="https://www.google.com/sorry/index")
    outcome, _ = refreshing_search(config, [sorry], refresher, limiter)
    assert outcome == Captcha("Google")
    assert refresher.calls == []
    assert limiter.pauses == [("a captcha", 600)]


def test_refreshed_cookies_ride_along_on_later_searches(config: Config) -> None:
    newer_sid = cookie("SID", "newer", ".google.com")
    refresher = FakeRefresher("unused", stored=[SG_SS, newer_sid])
    outcome, sent = refreshing_search(config, [response(numbered(0, 3))], refresher, FakeLimiter())
    assert isinstance(outcome, SearchResults)
    assert sent[0]["SG_SS"] == "token"
    assert sent[0]["SID"] == "newer"  # the refreshed cookie replaces the browser's
    assert refresher.calls == []


def test_no_request_while_searches_are_paused(config: Config) -> None:
    seen: list[str] = []
    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader({}, seen),
        limiter=FakeLimiter(paused=Paused(seconds_left=61, cause="a captcha")),
    )
    assert outcome == SearchPaused("Google", "a captcha", 2)
    assert seen == []


def test_network_failure_is_returned(config: Config) -> None:
    pages: dict[int, HttpResponse | DownloadFailure] = {
        0: NetworkFailure("https://www.google.com", "timeout")
    }
    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader(pages, []),
        limiter=FakeLimiter(),
    )
    assert outcome == NetworkFailure("https://www.google.com", "timeout")


def test_search_requires_google_login(config: Config) -> None:
    def anonymous(browser: str, domain: str, cookie_file: Path | None) -> list[Cookie]:
        return [cookie("NID", "n", ".google.com"), cookie("SID", "x", ".notgoogle.com")]

    outcome = search("q", 1, config, cookie_loader=anonymous, limiter=FakeLimiter())
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
    assert json.loads(state.read_text())["last"] > 0


def test_rate_limiter_reads_the_older_state_format(tmp_path: Path) -> None:
    state = tmp_path / "last"
    state.write_text("999.5")
    sleeps: list[float] = []
    RateLimiter(state, 2.0, now=lambda: 1000.0, sleep=sleeps.append).wait()
    assert sleeps == [pytest.approx(1.5)]


def test_pause_is_shared_and_ends(tmp_path: Path) -> None:
    clock = [1000.0]
    state = tmp_path / "last"

    def limiter() -> RateLimiter:  # a fresh object, like another process
        return RateLimiter(state, 2.0, now=lambda: clock[0], sleep=lambda s: None)

    limiter().pause("a captcha", 600)
    assert limiter().wait() == Paused(600, "a captcha")
    assert limiter().paused() == Paused(600, "a captcha")
    clock[0] += 599
    assert limiter().wait() == Paused(1, "a captcha")
    clock[0] += 2
    assert limiter().wait() is None
    assert limiter().paused() is None
    assert json.loads(state.read_text())["last"] == clock[0]


class QueueLimiter(FakeLimiter):
    """Answers wait() from a list, then with None (a free slot)."""

    def __init__(self, answers: list[Paused | OverBudget | None]) -> None:
        super().__init__()
        self.answers = answers

    def wait(self) -> Paused | OverBudget | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.calls += 1
        return self.answers.pop(0) if self.answers else None


def test_a_used_up_budget_sends_nothing(config: Config) -> None:
    seen: list[str] = []
    limiter = QueueLimiter([OverBudget(20, 95.2)])
    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader({0: response(numbered(0, 9))}, seen),
        limiter=limiter,
        budget_wait=30,
    )
    assert outcome == SearchBudgetUsed("Google", 20, 96)
    assert seen == []
    assert limiter.pauses == []  # Google didn't block anything


def test_a_search_waits_for_a_budget_slot_that_frees_up_soon(config: Config) -> None:
    seen: list[str] = []
    sleeps: list[float] = []
    limiter = QueueLimiter([OverBudget(20, 4.0), OverBudget(20, 3.0)])
    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader({0: response(numbered(0, 9))}, seen),
        limiter=limiter,
        budget_wait=30,
        sleep=sleeps.append,
    )
    assert isinstance(outcome, SearchResults)
    assert sleeps == [4.0, 3.0]
    assert len(seen) == 1
    # Without time to wait (a search API can answer instead), it says so right away.
    limiter = QueueLimiter([OverBudget(20, 4.0)])
    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=make_downloader({0: response(numbered(0, 9))}, seen),
        limiter=limiter,
        budget_wait=0,
        sleep=sleeps.append,
    )
    assert outcome == SearchBudgetUsed("Google", 20, 4)


def test_rate_limiter_budget_is_a_rolling_window(tmp_path: Path) -> None:
    clock = [1000.0]
    state = tmp_path / "state"

    def limiter() -> RateLimiter:  # a new process each time, sharing the state file
        return RateLimiter(state, 0, budget=3, now=lambda: clock[0], sleep=lambda s: None)

    for step in range(3):
        clock[0] = 1000.0 + 100 * step
        assert limiter().wait() is None
    clock[0] = 1250.0
    assert limiter().wait() == OverBudget(3, 350.0)  # the first one leaves the window at 1600
    assert limiter().requests_in_window() == 3
    clock[0] = 1601.0
    assert limiter().wait() is None
    assert limiter().requests_in_window() == 3
    assert RateLimiter(state, 0, budget=0, now=lambda: clock[0]).wait() is None  # 0: no limit


def test_a_check_whose_retry_isnt_sent_still_pauses(config: Config) -> None:
    refresher = FakeRefresher(Refreshed([SG_SS], "headless firefox, 0.9 s"))
    limiter = QueueLimiter([None, OverBudget(20, 300.0)])  # the refresh took the last slot
    outcome, sent = refreshing_search(config, [JS_CHECK], refresher, limiter)
    assert outcome == JavaScriptWall("Google")
    assert len(sent) == 1
    assert limiter.pauses == [("its JavaScript check", 600)]
    assert refresher.lines == [
        "headless firefox, 0.9 s; the retry wasn't sent: the Google budget was used up"
    ]


def test_an_error_status_is_reported_and_doesnt_pause(config: Config) -> None:
    error = HttpResponse("https://www.google.com/search?q=x", 503, "", {}, b"<p>enablejs</p>")
    limiter = FakeLimiter()

    def downloader(url: str, **kwargs: object) -> HttpResponse:
        return error

    outcome = search(
        "q",
        1,
        config,
        cookie_loader=signed_in_loader,
        downloader=downloader,
        limiter=limiter,
        refresher=None,
    )
    assert outcome == SearchHttpError("Google", 503)
    assert limiter.pauses == []  # an error page isn't a JavaScript check


def test_a_reused_refresh_with_nothing_new_isnt_retried(config: Config) -> None:
    """Another search's refresh, whose cookies this search had already sent."""
    refresher = FakeRefresher(Refreshed([SG_SS], "reused a refresh 20 s old"), stored=[SG_SS])
    limiter = FakeLimiter()
    outcome, sent = refreshing_search(config, [JS_CHECK], refresher, limiter)
    assert outcome == JavaScriptWall("Google")
    assert len(sent) == 1
    assert limiter.pauses == [("its JavaScript check", 600)]
    assert refresher.lines == [
        "reused a refresh 20 s old; not retried: the blocked search already sent them"
    ]
