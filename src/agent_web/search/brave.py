"""Brave Search API: answers when Google can't, or every search if search.engine = "brave".

Results come from Brave's own index through its official API, with the user's key. Only the query
goes to Brave: no cookies. https://api-dashboard.search.brave.com/documentation/services/web-search
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

from agent_web.config import Config
from agent_web.failures import Failure, SearchApiError
from agent_web.http import DownloadFailure, HttpResponse, download
from agent_web.invisible import reveal
from agent_web.jsonvalue import as_dict, as_list, get_str
from agent_web.search import RESULTS_PER_PAGE, SearchResult
from agent_web.urls import normalize

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SERVICE = "Brave"
# Brave limits requests per second (HTTP 429): parallel agents retry after this long, this often.
RETRY_SECONDS = 1.1
RETRIES = 2
MAX_DETAIL = 200
# Brave reports its limits in these headers on every answer, e.g. "1, 15000" (per second, month).
QUOTA_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
_TAG = re.compile(r"<[^>]*>")

Downloader = Callable[..., HttpResponse | DownloadFailure]


def search_url(query: str, page: int) -> str:
    """`page` counts from 1; Brave's offset is the page number from 0."""
    params = {"q": query, "count": str(RESULTS_PER_PAGE), "offset": str(page - 1)}
    return f"{SEARCH_URL}?{urlencode(params)}"


def _clean(text: str) -> str:
    """Brave marks the matched words with <strong> and escapes entities."""
    return reveal(" ".join(html.unescape(_TAG.sub("", text)).split()))[0]


def parse_results(payload: bytes) -> list[SearchResult] | None:
    """The web results in Brave's JSON answer; None if it isn't JSON."""
    try:
        data = as_dict(json.loads(payload))
    except ValueError:
        return None
    if data is None:
        return None
    web = as_dict(data.get("web")) or {}
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in as_list(web.get("results")) or []:
        entry = as_dict(item) or {}
        url = get_str(entry, "url")
        if not url.startswith(("https://", "http://")) or normalize(url) in seen:
            continue
        seen.add(normalize(url))
        title = _clean(get_str(entry, "title")) or url
        results.append(SearchResult(title, url, _clean(get_str(entry, "description"))))
    return results


def _error_detail(payload: bytes) -> str:
    """Brave's error answers look like {"error": {"code": ..., "detail": ...}}."""
    try:
        data = as_dict(json.loads(payload))
    except ValueError:
        return ""
    error = as_dict((data or {}).get("error")) or {}
    detail = get_str(error, "detail") or get_str(error, "code")
    return " ".join(detail.split())[:MAX_DETAIL]


def quota_path(config: Config) -> Path:
    return config.cache_dir / "brave-quota.json"


def _save_quota(path: Path, response: HttpResponse, now: float) -> None:
    quota = {name: response.headers[name] for name in QUOTA_HEADERS if name in response.headers}
    if not quota:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": now, **quota}), encoding="utf-8")


def _numbers(header: str) -> list[int]:
    """ "50, 0" -> [50, 0]; [] if it isn't a list of numbers."""
    parts = [part.strip() for part in header.split(",")]
    return [int(part) for part in parts] if all(part.isdigit() for part in parts) else []


def last_quota(config: Config) -> str | None:
    """What Brave's limit headers said in its last answer, for `web doctor`."""
    try:
        data = as_dict(json.loads(quota_path(config).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if data is None:
        return None
    at = data.get("at")
    when = time.strftime("%H:%M UTC", time.gmtime(at)) if isinstance(at, int | float) else "?"
    limits = _numbers(get_str(data, "x-ratelimit-limit"))
    remaining = _numbers(get_str(data, "x-ratelimit-remaining"))
    if len(limits) != 2 or len(remaining) != 2:
        return f"limit headers at {when}: {get_str(data, 'x-ratelimit-limit') or '?'}"
    per_second, per_month = limits
    if per_month == 0:  # Brave's way of saying the plan has no monthly cap
        return (
            f"up to {per_second} searches a second, no monthly cap: searches are paid from "
            f"credit (usage: api-dashboard.search.brave.com), as of {when}"
        )
    return f"{remaining[1]} of {per_month} searches left this month, as of {when}"


def search(
    query: str,
    page: int,
    config: Config,
    key: str,
    *,
    downloader: Downloader = download,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> list[SearchResult] | Failure:
    """The results on one Brave results page (1 is the first)."""
    url = search_url(query, page)
    attempt = 0
    while True:
        response = downloader(
            url,
            timeout=config.timeout,
            max_bytes=config.max_download_bytes,
            allow_private=config.allow_private_networks,
            extra_headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        if not isinstance(response, HttpResponse):
            return response
        _save_quota(quota_path(config), response, now())
        if response.status != 429 or attempt == RETRIES:
            break
        attempt += 1
        sleep(RETRY_SECONDS)
    if response.status != 200:
        return SearchApiError(SERVICE, response.status, _error_detail(response.content))
    results = parse_results(response.content)
    if results is None:
        return SearchApiError(SERVICE, response.status, "the answer wasn't JSON")
    return results
