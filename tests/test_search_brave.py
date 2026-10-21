"""The Brave Search API client, with a fake downloader."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent_web.config import Config
from agent_web.failures import EXIT_USAGE, NetworkFailure, SearchApiError, describe, exit_code
from agent_web.http import DownloadFailure, HttpResponse
from agent_web.search import SearchResult
from agent_web.search.brave import last_quota, parse_results, search

QUOTA = {
    "x-ratelimit-limit": "50, 0",
    "x-ratelimit-remaining": "49, 0",
    "x-ratelimit-reset": "1, 1419704",
}


def answer(status: int, payload: object, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        url="https://api.search.brave.com/res/v1/web/search",
        status=status,
        reason="",
        headers={"content-type": "application/json", **(headers or {})},
        content=json.dumps(payload).encode(),
    )


RESULTS = {
    "query": {"original": "q", "more_results_available": True},
    "web": {
        "results": [
            {
                "title": "SQLite <strong>WAL</strong> &amp; you",
                "url": "https://sqlite.org/wal.html",
                "description": "The <strong>write-ahead</strong>\n log &quot;mode&quot;.",
            },
            {"title": "Again", "url": "https://sqlite.org/wal.html#x", "description": "dup"},
            {"title": "Not a page", "url": "javascript:alert(1)", "description": ""},
            {"title": "", "url": "https://b.example/", "description": "tag\U000e0041\U000e0042"},
        ]
    },
}


def test_parse_results_cleans_markup_and_skips_duplicates() -> None:
    found = parse_results(json.dumps(RESULTS).encode())
    assert found == [
        SearchResult(
            "SQLite WAL & you", "https://sqlite.org/wal.html", 'The write-ahead log "mode".'
        ),
        SearchResult("https://b.example/", "https://b.example/", "tag⟦hidden on page: AB⟧"),
    ]
    assert parse_results(b"{}") == []
    assert parse_results(b"not json") is None
    assert parse_results(b"[1]") is None


class Downloads:
    def __init__(self, *answers: HttpResponse | DownloadFailure) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **kwargs: object) -> HttpResponse | DownloadFailure:
        self.calls.append((url, kwargs))
        return self.answers.pop(0)


def test_one_request_per_page_with_the_key_and_no_cookies(config: Config) -> None:
    downloads = Downloads(answer(200, RESULTS, QUOTA))
    found = search("sqlite wal", 3, config, "KEY", downloader=downloads, now=lambda: 0.0)
    assert isinstance(found, list)
    assert len(found) == 2
    [(url, kwargs)] = downloads.calls
    assert parse_qs(urlsplit(url).query) == {"q": ["sqlite wal"], "count": ["10"], "offset": ["2"]}
    assert kwargs["extra_headers"] == {"X-Subscription-Token": "KEY", "Accept": "application/json"}
    assert "cookie_source" not in kwargs
    assert last_quota(config) == (
        "limits 50, 0 (per second, per month); remaining 49, 0 at 00:00 UTC"
    )


def test_too_many_requests_at_once_are_retried(config: Config) -> None:
    sleeps: list[float] = []
    busy = answer(429, {"error": {"code": "RATE_LIMITED", "detail": "Request rate limit exceeded"}})
    downloads = Downloads(busy, busy, answer(200, RESULTS))
    found = search("q", 1, config, "KEY", downloader=downloads, sleep=sleeps.append)
    assert isinstance(found, list)
    assert sleeps == [1.1, 1.1]
    downloads = Downloads(busy, busy, busy)
    failed = search("q", 1, config, "KEY", downloader=downloads, sleep=sleeps.append)
    assert failed == SearchApiError("Brave", 429, "Request rate limit exceeded")
    assert isinstance(failed, SearchApiError)
    assert "the month's credit is used up" in describe(failed)


def test_errors(config: Config, tmp_path: Path) -> None:
    rejected = answer(
        422, {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": "The token is invalid."}}
    )
    failed = search("q", 1, config, "BAD", downloader=Downloads(rejected))
    assert failed == SearchApiError("Brave", 422, "The token is invalid.")
    assert isinstance(failed, SearchApiError)
    assert exit_code(failed) == EXIT_USAGE
    assert "Check the Brave Search API key" in describe(failed)
    garbage = HttpResponse("https://api.search.brave.com/", 200, "", {}, b"<html>")
    assert search("q", 1, config, "K", downloader=Downloads(garbage)) == SearchApiError(
        "Brave", 200, "the answer wasn't JSON"
    )
    offline = NetworkFailure("https://api.search.brave.com/", "timeout")
    assert search("q", 1, config, "K", downloader=Downloads(offline)) == offline
    assert last_quota(Config(cache_dir=tmp_path / "none")) is None
