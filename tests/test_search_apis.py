"""The search API adapters, with a fake downloader and answers shaped like each API's docs."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_web.config import API_KEY_ENV, Config
from agent_web.failures import NetworkFailure, SearchApiError
from agent_web.http import DownloadFailure, HttpResponse
from agent_web.search import SearchResult
from agent_web.search.apis import (
    ALL_OPERATORS,
    APIS,
    PHRASES,
    operators_used,
    search,
    split_sites,
)
from agent_web.search.dispatch import setback


def answer(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        url="https://api.example/",
        status=status,
        reason="",
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


class Downloads:
    def __init__(self, *answers: HttpResponse | DownloadFailure) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **kwargs: object) -> HttpResponse | DownloadFailure:
        self.calls.append((url, kwargs))
        return self.answers.pop(0)

    def body(self, call: int = 0) -> dict[str, object]:
        post = self.calls[call][1]["post"]
        assert isinstance(post, bytes)
        return json.loads(post)


def results(n: int, start: int = 0) -> Rows:
    return [
        (f"T{i}", f"https://site{i}.example/", f"<strong>s{i}</strong>")
        for i in range(start, start + n)
    ]


# Each API's answer, built the way its documentation shows, holding `rows`.
Rows = list[tuple[str, str, str]]
ANSWERS: dict[str, Callable[[Rows], object]] = {
    "brave": lambda rows: {
        "web": {"results": [{"title": t, "url": u, "description": s} for t, u, s in rows]}
    },
    "serpapi": lambda rows: {
        "organic_results": [{"title": t, "link": u, "snippet": s} for t, u, s in rows]
    },
    "tavily": lambda rows: {
        "results": [{"title": t, "url": u, "content": s, "score": 0.9} for t, u, s in rows]
    },
    "exa": lambda rows: {
        "results": [{"title": t, "url": u, "highlights": [s, "more"]} for t, u, s in rows]
    },
    "parallel": lambda rows: {
        "results": [{"title": t, "url": u, "excerpts": [s]} for t, u, s in rows]
    },
    "firecrawl": lambda rows: {
        "success": True,
        "data": {"web": [{"title": t, "url": u, "description": s} for t, u, s in rows]},
    },
    "langsearch": lambda rows: {
        "code": 200,
        "data": {"webPages": {"value": [{"name": t, "url": u, "snippet": s} for t, u, s in rows]}},
    },
    "serper": lambda rows: {"organic": [{"title": t, "link": u, "snippet": s} for t, u, s in rows]},
}


def test_every_api_has_an_answer_shape_and_a_key_variable() -> None:
    assert list(APIS) == list(ANSWERS) == list(API_KEY_ENV)
    for name, api in APIS.items():
        assert api.title.lower() == name  # --engine takes the title in lower case


@pytest.mark.parametrize("name", list(APIS))
def test_first_page(config: Config, name: str) -> None:
    downloads = Downloads(answer(200, ANSWERS[name](results(10))))
    found = search(APIS[name], "sqlite wal", 1, config, "KEY", downloader=downloads)
    assert isinstance(found, list)
    assert found[0].title == "T0"
    assert found[0].url == "https://site0.example/"
    assert found[0].snippet.startswith("s0")  # markup removed
    assert len(found) == 10
    [(url, kwargs)] = downloads.calls
    sent = json.dumps(kwargs.get("extra_headers")) + url
    assert "KEY" in sent  # the key goes in a header (or SerpAPI's URL)...
    assert "cookie_source" not in kwargs  # ...and no cookies go anywhere


@pytest.mark.parametrize("name", [n for n, api in APIS.items() if api.max_page >= 2])
def test_second_page(config: Config, name: str) -> None:
    api = APIS[name]
    rows = results(20) if api.from_start else results(10, start=10)
    downloads = Downloads(answer(200, ANSWERS[name](rows)))
    found = search(api, "q", 2, config, "KEY", downloader=downloads)
    assert isinstance(found, list)
    assert [r.title for r in found] == [f"T{i}" for i in range(10, 20)]


def test_requests() -> None:
    brave = APIS["brave"].build("q x", 3, "K")
    assert parse_qs(urlsplit(brave.url).query) == {"q": ["q x"], "count": ["10"], "offset": ["2"]}
    assert brave.headers == {"X-Subscription-Token": "K"}
    serpapi = APIS["serpapi"].build("q", 2, "K")
    assert parse_qs(urlsplit(serpapi.url).query)["start"] == ["10"]
    tavily = APIS["tavily"].build("jobs site:a.com -site:b.org", 2, "K")
    assert tavily.headers == {"Authorization": "Bearer K"}
    assert tavily.body == {
        "query": "jobs",
        "max_results": 20,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "include_domains": ["a.com"],
        "exclude_domains": ["b.org"],
    }
    exa = APIS["exa"].build("(site:a.com OR site:b.com) engineer", 1, "K")
    assert exa.body is not None
    assert (exa.body["query"], exa.body["includeDomains"]) == ("engineer", ["a.com", "b.com"])
    assert APIS["serper"].build("q", 4, "K").body == {"q": "q", "page": 4, "num": 10}
    assert APIS["parallel"].build("q", 1, "K").body == {"search_queries": ["q"]}


def test_split_sites() -> None:
    assert split_sites("sqlite -site:sqlite.org") == ("sqlite", [], ["sqlite.org"])
    assert split_sites('(site:a.io OR site:B.io) "new grad" OR junior') == (
        '"new grad" OR junior',
        ["a.io", "b.io"],
        [],
    )
    assert split_sites("site:a.io") == ("site:a.io", ["a.io"], [])  # nothing left: keep it all
    assert split_sites("website:x campsite:y") == ("website:x campsite:y", [], [])


def test_duplicates_bad_urls_and_long_snippets_are_cleaned(config: Config) -> None:
    rows = [
        ("A", "https://a.example/x", "s" * 900),
        ("A again", "https://a.example/x#frag", ""),
        ("Script", "javascript:alert(1)", ""),
        ("", "https://b.example/", "tag\U000e0041"),
    ]
    downloads = Downloads(answer(200, ANSWERS["brave"](rows)))
    found = search(APIS["brave"], "q", 1, config, "K", downloader=downloads)
    assert isinstance(found, list)
    assert [r.url for r in found] == ["https://a.example/x", "https://b.example/"]
    assert len(found[0].snippet) <= 402
    assert found[1] == SearchResult(
        "https://b.example/", "https://b.example/", "tag⟦hidden on page: A⟧"
    )


def test_too_many_requests_at_once_are_retried(config: Config) -> None:
    sleeps: list[float] = []
    busy = answer(429, {"error": {"code": "RATE_LIMITED", "detail": "Request rate limit exceeded"}})
    downloads = Downloads(busy, busy, answer(200, ANSWERS["brave"](results(3))))
    found = search(APIS["brave"], "q", 1, config, "K", downloader=downloads, sleep=sleeps.append)
    assert isinstance(found, list)
    assert sleeps == [1.1, 1.1]
    downloads = Downloads(busy, busy, busy)
    failed = search(APIS["brave"], "q", 1, config, "K", downloader=downloads, sleep=sleeps.append)
    assert failed == SearchApiError("Brave", 429, "Request rate limit exceeded (RATE_LIMITED)")


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        (
            {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": "The token is invalid."}},
            "The token is invalid. (SUBSCRIPTION_TOKEN_INVALID)",
        ),
        (
            {"detail": {"error": "This request exceeds your plan's set usage limit."}},
            "This request exceeds your plan's set usage limit.",
        ),
        (
            {"error": "Your account has run out of searches."},
            "Your account has run out of searches.",
        ),
        ({"message": "Not enough credits", "statusCode": 400}, "Not enough credits"),
        ("plain", ""),
    ],
)
def test_error_details(config: Config, payload: object, detail: str) -> None:
    downloads = Downloads(answer(432, payload))
    assert search(APIS["tavily"], "q", 1, config, "K", downloader=downloads) == SearchApiError(
        "Tavily", 432, detail
    )


def test_unexpected_answers(config: Config) -> None:
    garbage = HttpResponse("https://api.example/", 200, "", {}, b"<html>")
    assert search(APIS["brave"], "q", 1, config, "K", downloader=Downloads(garbage)) == (
        SearchApiError("Brave", 200, "the answer wasn't JSON")
    )
    error_inside = answer(200, {"code": 401, "msg": "invalid api key"})
    assert search(APIS["langsearch"], "q", 1, config, "K", downloader=Downloads(error_inside)) == (
        SearchApiError("LangSearch", 200, "invalid api key")
    )
    assert search(APIS["brave"], "q", 1, config, "K", downloader=Downloads(answer(200, {}))) == []


def test_network_failures_never_show_the_key(config: Config) -> None:
    url = APIS["serpapi"].build("q", 1, "SECRET").url
    offline = NetworkFailure(url, f"could not resolve {url}")
    failed = search(APIS["serpapi"], "q", 1, config, "SECRET", downloader=Downloads(offline))
    assert isinstance(failed, NetworkFailure)
    assert "SECRET" not in failed.url + failed.reason


def test_retry_after_is_followed_when_short_and_reported_when_long(config: Config) -> None:
    sleeps: list[float] = []

    def busy(seconds: str) -> HttpResponse:
        response = answer(429, {"message": "Too many requests"})
        return HttpResponse(
            response.url, 429, "", {**response.headers, "retry-after": seconds}, response.content
        )

    rows = ANSWERS["tavily"](results(2))
    downloads = Downloads(busy("2"), answer(200, rows))
    found = search(APIS["tavily"], "q", 1, config, "K", downloader=downloads, sleep=sleeps.append)
    assert isinstance(found, list)
    assert sleeps == [2.0]
    downloads = Downloads(busy("30"))
    failed = search(APIS["tavily"], "q", 1, config, "K", downloader=downloads, sleep=sleeps.append)
    assert failed == SearchApiError("Tavily", 429, "Too many requests", retry_after=30.0)
    assert sleeps == [2.0]  # not retried: it asked for longer than agent-web waits


def test_firecrawl_without_a_key_sends_no_authorization(config: Config) -> None:
    assert "Authorization" not in APIS["firecrawl"].build("q", 1, "").headers
    downloads = Downloads(NetworkFailure("https://api.firecrawl.dev/v2/search", "timeout"))
    failed = search(APIS["firecrawl"], "q", 1, config, "", downloader=downloads)
    assert failed == NetworkFailure("https://api.firecrawl.dev/v2/search", "timeout")


def test_quota_headers_tell_a_used_up_quota_from_a_burst(config: Config) -> None:
    def busy(limit: str, remaining: str) -> HttpResponse:
        body = {
            "type": "ErrorResponse",
            "error": {"code": "RATE_LIMITED", "detail": "Request rate limit exceeded for plan."},
        }
        response = answer(429, body)
        headers = {
            **response.headers,
            "x-ratelimit-limit": limit,
            "x-ratelimit-remaining": remaining,
        }
        return HttpResponse(response.url, 429, "", headers, response.content)

    sleeps: list[float] = []
    used_up = busy("50, 15000", "50, 0")
    failed = search(
        APIS["brave"], "q", 1, config, "K", downloader=Downloads(used_up), sleep=sleeps.append
    )
    assert failed == SearchApiError(
        "Brave",
        429,
        "Request rate limit exceeded for plan. (RATE_LIMITED); its quota is used up",
        used_up=True,
    )
    assert sleeps == []  # not retried
    assert isinstance(failed, SearchApiError)
    assert setback(failed, 0.0)[0] == "is out of credit"
    burst = busy("50, 0", "0, 0")  # a credit plan: no monthly cap, just this second's limit
    downloads = Downloads(burst, burst, burst)
    failed = search(APIS["brave"], "q", 1, config, "K", downloader=downloads, sleep=sleeps.append)
    assert isinstance(failed, SearchApiError)
    assert sleeps == [1.1, 1.1]
    assert setback(failed, 0.0)[0] == "is limiting requests"


JOBS = '(site:a.io OR site:b.io) "new grad" OR junior -senior filetype:pdf intitle:salary'


def test_operators_used() -> None:
    assert operators_used("sqlite wal") == frozenset()
    assert operators_used("(site:a.io OR site:b.io) jobs -site:c.io") == frozenset()  # sites only
    assert operators_used(JOBS) == ALL_OPERATORS
    assert operators_used("write-ahead log") == frozenset()  # a hyphen inside a word
    assert operators_used('"exact words" here') == {PHRASES}


def test_queries_are_sent_as_written(config: Config) -> None:
    """Operators an engine doesn't understand stay in the query; only site: becomes a filter."""
    downloads = Downloads(answer(200, ANSWERS["exa"]([])))
    search(APIS["exa"], JOBS, 1, config, "K", downloader=downloads)
    body = downloads.body()
    assert body["query"] == '"new grad" OR junior -senior filetype:pdf intitle:salary'
    assert body["includeDomains"] == ["a.io", "b.io"]
    downloads = Downloads(answer(200, ANSWERS["brave"]([])))
    search(APIS["brave"], JOBS, 1, config, "K", downloader=downloads)
    assert parse_qs(urlsplit(downloads.calls[0][0]).query)["q"] == [JOBS]
