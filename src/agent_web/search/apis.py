"""Search APIs that answer when Google can't, mostly on free tiers, tried in order.

Each Api says how to ask for one results page (one HTTP request) and where the title, URL and
snippet of each result are in its JSON answer. Only the query goes to the provider, with the
user's key; never cookies. Search operators: `site:` and `-site:` become the provider's domain
filters where it has them; the rest of the query is passed as written.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from agent_web.config import Config
from agent_web.failures import Failure, NetworkFailure, SearchApiError
from agent_web.http import DownloadFailure, HttpResponse, download
from agent_web.invisible import reveal
from agent_web.jsonvalue import as_dict, as_list, get_str
from agent_web.search import MAX_PAGE, RESULTS_PER_PAGE, SearchResult
from agent_web.urls import normalize

MAX_SNIPPET = 400
MAX_DETAIL = 200
# HTTP 429 often means "too many requests this second": retried this often, this long apart (or
# as long as its Retry-After says, if that's at most MAX_RETRY_WAIT).
RETRIES = 2
RETRY_SECONDS = 1.1
MAX_RETRY_WAIT = 5.0

Downloader = Callable[..., HttpResponse | DownloadFailure]
# What an API's answer holds for each result: title, URL, snippet.
Found = list[tuple[str, str, str]]


@dataclass(frozen=True)
class Request:
    url: str
    headers: dict[str, str]
    body: dict[str, object] | None = None  # POSTed as JSON; None: GET


@dataclass(frozen=True)
class Api:
    name: str  # in the config and --engine: "serpapi"
    title: str  # in the output: "SerpAPI"
    free: str  # its free tier, for `web doctor`
    signup: str  # where to get a key
    build: Callable[[str, int, str], Request]  # query, page, key
    parse: Callable[[object], Found | None]  # None: not the expected answer
    # The Google-style operators it understands (OPERATORS): queries using others are sent to
    # engines that do first, and the note warns when this one answers anyway.
    operators: frozenset[str]
    # Its documented rate limit on the free tier, as (requests, seconds); agent-web keeps under it.
    rate: tuple[int, float]
    max_page: int = MAX_PAGE
    # Without an offset parameter, page N is asked for as the first N pages in one answer.
    from_start: bool = False

    @property
    def rate_text(self) -> str:
        count, seconds = self.rate
        unit = {1: "a second", 60: "a minute", 3600: "an hour"}.get(
            int(seconds), f"per {seconds:g} s"
        )
        return f"{count} {unit}"


_SITE = re.compile(r"(?<![^\s(])(-?)site:([^\s()]+)", re.IGNORECASE)


# Kinds of Google-style operators besides site: (which every API handles, natively or as a filter).
PHRASES = "quoted phrases"
BOOLEAN = "OR/AND/NOT"
EXCLUDE = "-word exclusions"
FIELDS = "operators like filetype: and intitle:"
OPERATORS = (PHRASES, BOOLEAN, EXCLUDE, FIELDS)
ALL_OPERATORS = frozenset(OPERATORS)

_PHRASE = re.compile(r'"[^"]*"')
_BOOLEAN = re.compile(r"(?<!\S)(?:OR|AND|NOT)(?!\S)|[()|]")
_EXCLUDE = re.compile(r'(?<![^\s(])-(?!site:)(?:"[^"]*"|[^\s()]+)', re.IGNORECASE)
_FIELD = re.compile(
    r"(?<![^\s(])-?(?:filetype|ext|intitle|allintitle|inurl|allinurl|intext|allintext|inbody|"
    r"inpage|related|before|after):",
    re.IGNORECASE,
)
_SITES_ONLY_GROUP = re.compile(r"\(\s*(?:OR\s*)*\)")


def operators_used(query: str) -> frozenset[str]:
    """The kinds of operators (OPERATORS) a query uses, besides site:."""
    rest = _SITES_ONLY_GROUP.sub(" ", _SITE.sub(" ", query))
    patterns = ((PHRASES, _PHRASE), (BOOLEAN, _BOOLEAN), (EXCLUDE, _EXCLUDE), (FIELDS, _FIELD))
    return frozenset(kind for kind, pattern in patterns if pattern.search(rest))


def split_sites(query: str) -> tuple[str, list[str], list[str]]:
    """`site:` and `-site:` taken out of the query, for APIs with domain filters:
    (rest of the query, domains to include, domains to exclude)."""
    include: list[str] = []
    exclude: list[str] = []

    def take(match: re.Match[str]) -> str:
        (exclude if match.group(1) else include).append(match.group(2).lower())
        return " "

    rest = _SITE.sub(take, query)
    rest = _SITES_ONLY_GROUP.sub(" ", rest)  # groups that held only sites
    rest = re.sub(r"\bOR(?:\s+OR\b)+", "OR", rest)
    rest = re.sub(r"^\s*OR\b|\bOR\s*$", "", rest.strip())
    rest = " ".join(rest.split())
    return (rest or query), include, exclude


def _domains(query: str, include_key: str, exclude_key: str) -> tuple[str, dict[str, object]]:
    rest, include, exclude = split_sites(query)
    filters: dict[str, object] = {}
    if include:
        filters[include_key] = include
    if exclude:
        filters[exclude_key] = exclude
    return rest, filters


def _items(data: object, *path: str) -> list[dict[str, object]]:
    """The result objects at `path` in the answer; none if it isn't there (APIs leave the list
    out when nothing matched)."""
    for key in path:
        data = (as_dict(data) or {}).get(key)
    return [item for raw in as_list(data) or [] if (item := as_dict(raw)) is not None]


def _snippet(item: dict[str, object], keys: tuple[str, ...]) -> str:
    """The first of `keys` that holds text; some APIs give a list of excerpts instead."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
        excerpts = [e for e in as_list(value) or [] if isinstance(e, str) and e.strip()]
        if excerpts:
            return " … ".join(excerpts)
    return ""


def _fields(items: list[dict[str, object]], title: str, url: str, *snippet: str) -> Found:
    found: Found = []
    for item in items:
        found.append((get_str(item, title), get_str(item, url), _snippet(item, snippet)))
    return found


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _brave(query: str, page: int, key: str) -> Request:
    params = {"q": query, "count": str(RESULTS_PER_PAGE), "offset": str(page - 1)}
    return Request(
        f"https://api.search.brave.com/res/v1/web/search?{urlencode(params)}",
        {"X-Subscription-Token": key},
    )


def _serpapi(query: str, page: int, key: str) -> Request:
    params = {
        "engine": "google",
        "q": query,
        "start": str((page - 1) * RESULTS_PER_PAGE),
        "num": str(RESULTS_PER_PAGE),
    }
    # SerpAPI takes the key only as a parameter; it's blanked out of error messages.
    return Request(f"https://serpapi.com/search.json?{urlencode({**params, 'api_key': key})}", {})


def _tavily(query: str, page: int, key: str) -> Request:
    rest, domains = _domains(query, "include_domains", "exclude_domains")
    body: dict[str, object] = {
        "query": rest,
        "max_results": page * RESULTS_PER_PAGE,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        **domains,
    }
    if '"' in rest:
        body["exact_match"] = True  # Tavily's way of honoring quoted phrases
    return Request("https://api.tavily.com/search", _bearer(key), body)


def _exa(query: str, page: int, key: str) -> Request:
    rest, domains = _domains(query, "includeDomains", "excludeDomains")
    body: dict[str, object] = {
        "query": rest,
        "numResults": page * RESULTS_PER_PAGE,
        "type": "auto",
        "contents": {"highlights": True},
        **domains,
    }
    return Request("https://api.exa.ai/search", {"x-api-key": key}, body)


def _parallel(query: str, page: int, key: str) -> Request:
    return Request(
        "https://api.parallel.ai/v1/search", {"x-api-key": key}, {"search_queries": [query]}
    )


def _firecrawl(query: str, page: int, key: str) -> Request:
    rest, domains = _domains(query, "includeDomains", "excludeDomains")
    body: dict[str, object] = {
        "query": rest,
        "limit": page * RESULTS_PER_PAGE,
        "sources": [{"type": "web"}],
        **domains,
    }
    # Without a key, Firecrawl answers anyway, up to a daily cap per IP address.
    return Request("https://api.firecrawl.dev/v2/search", _bearer(key) if key else {}, body)


def _langsearch(query: str, page: int, key: str) -> Request:
    rest, domains = _domains(query, "includeDomains", "excludeDomains")
    body: dict[str, object] = {"query": rest, "count": page * RESULTS_PER_PAGE, **domains}
    return Request("https://api.langsearch.com/v1/web-search", _bearer(key), body)


def _serper(query: str, page: int, key: str) -> Request:
    body: dict[str, object] = {"q": query, "page": page, "num": RESULTS_PER_PAGE}
    return Request("https://google.serper.dev/search", {"X-API-KEY": key}, body)


def _langsearch_results(data: object) -> Found | None:
    if (as_dict(data) or {}).get("code") not in (200, None):
        return None  # LangSearch can report an error inside an HTTP 200 answer
    return _fields(_items(data, "data", "webPages", "value"), "name", "url", "snippet", "text")


# In the order they're tried by default (config.API_KEY_ENV has the same order).
APIS: dict[str, Api] = {
    api.name: api
    for api in (
        Api(
            "brave",
            "Brave",
            "$5 of credit a month (about 1,000 searches; a card is required)",
            "https://api-dashboard.search.brave.com",
            _brave,
            lambda data: _fields(_items(data, "web", "results"), "title", "url", "description"),
            ALL_OPERATORS,
            (50, 1),
        ),
        Api(
            "serpapi",
            "SerpAPI",
            "250 Google searches a month",
            "https://serpapi.com/users/sign_up",
            _serpapi,
            lambda data: _fields(_items(data, "organic_results"), "title", "link", "snippet"),
            ALL_OPERATORS,
            (50, 3600),
        ),
        Api(
            "tavily",
            "Tavily",
            "1,000 searches a month",
            "https://app.tavily.com",
            _tavily,
            lambda data: _fields(_items(data, "results"), "title", "url", "content"),
            frozenset({PHRASES}),
            (100, 60),
            max_page=2,
            from_start=True,
        ),
        Api(
            "exa",
            "Exa",
            "$10 of credit a month",
            "https://dashboard.exa.ai",
            _exa,
            lambda data: _fields(_items(data, "results"), "title", "url", "highlights", "text"),
            frozenset(),
            (10, 1),
            max_page=2,
            from_start=True,
        ),
        Api(
            "parallel",
            "Parallel",
            "$5 of credit a month",
            "https://platform.parallel.ai",
            _parallel,
            lambda data: _fields(_items(data, "results"), "title", "url", "excerpts"),
            frozenset(),
            (10, 1),  # documented as 600 a minute
            max_page=1,
        ),
        Api(
            "firecrawl",
            "Firecrawl",
            "500 searches a month with a key; without one, a daily cap per IP address",
            "https://www.firecrawl.dev/app",
            _firecrawl,
            lambda data: _fields(_items(data, "data", "web"), "title", "url", "description"),
            frozenset({PHRASES, EXCLUDE, FIELDS}),
            (10, 60),
            max_page=2,
            from_start=True,
        ),
        Api(
            "langsearch",
            "LangSearch",
            "free, with a daily allowance",
            "https://langsearch.com/dashboard",
            _langsearch,
            _langsearch_results,
            frozenset(),
            (5, 1),
            from_start=True,
        ),
        Api(
            "serper",
            "Serper",
            "2,500 Google searches once, at sign-up",
            "https://serper.dev",
            _serper,
            lambda data: _fields(_items(data, "organic"), "title", "link", "snippet"),
            ALL_OPERATORS,
            (50, 1),
        ),
    )
}

_TAG = re.compile(r"<[^>]*>")


def _clean(text: str) -> str:
    """Some APIs mark matched words with <strong> and escape entities."""
    return reveal(" ".join(html.unescape(_TAG.sub("", text)).split()))[0]


def _results(found: Found, api: Api, page: int) -> list[SearchResult]:
    if api.from_start:
        found = found[(page - 1) * RESULTS_PER_PAGE : page * RESULTS_PER_PAGE]
    results: list[SearchResult] = []
    seen: set[str] = set()
    for title, url, snippet in found:
        if not url.startswith(("https://", "http://")) or normalize(url) in seen:
            continue
        seen.add(normalize(url))
        snippet = _clean(snippet)
        if len(snippet) > MAX_SNIPPET:
            snippet = snippet[:MAX_SNIPPET].rsplit(" ", 1)[0] + " …"
        results.append(SearchResult(_clean(title) or url, url, snippet))
    return results


def _detail(payload: bytes) -> str:
    """The message in an error answer: {"error": {"detail": …, "code": …}}, {"detail": {"error":
    …}}, {"message": …}, {"msg": …} and the like, with its error code if it has one."""
    try:
        data: object = json.loads(payload)
    except ValueError:
        return ""
    for _ in range(3):
        item = as_dict(data)
        if item is None:
            break
        code = get_str(item, "code")
        for key in ("detail", "error", "message", "msg"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                text = " ".join(value.split())
                return (f"{text} ({code})" if code and code not in text else text)[:MAX_DETAIL]
        if code:
            return code[:MAX_DETAIL]
        data = item.get("error") or item.get("detail")
    return ""


def _counts(header: str) -> list[int]:
    """ "1, 15000" -> [1, 15000]; [] if it isn't a list of numbers."""
    parts = [part.strip() for part in header.split(",")]
    return [int(part) for part in parts] if all(part.isdigit() for part in parts) else []


def _quota_used_up(headers: dict[str, str]) -> bool:
    """Whether a longer limit window (per month, after the per-second one) has nothing left, by
    the X-RateLimit-Limit and -Remaining headers (Brave's: "1, 15000"; a limit of 0: no cap)."""
    limits = _counts(headers.get("x-ratelimit-limit", ""))
    remaining = _counts(headers.get("x-ratelimit-remaining", ""))
    return len(limits) == len(remaining) > 1 and any(
        limit > 0 and left == 0 for limit, left in zip(limits[1:], remaining[1:], strict=True)
    )


def _retry_after(value: str) -> float:
    """Seconds a Retry-After header asks to wait: a number of seconds or an HTTP date."""
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return 0.0


def _hide(failure: DownloadFailure, key: str) -> DownloadFailure:
    """Network errors quote the URL, which holds the key for SerpAPI."""
    if isinstance(failure, NetworkFailure) and key:
        return NetworkFailure(failure.url.replace(key, "…"), failure.reason.replace(key, "…"))
    return failure


def search(
    api: Api,
    query: str,
    page: int,
    config: Config,
    key: str,
    *,
    downloader: Downloader = download,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SearchResult] | Failure:
    """One results page (1 is the first) from `api`."""
    request = api.build(query, page, key)
    headers = {"Accept": "application/json", **request.headers}
    body = None
    if request.body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(request.body).encode()
    attempt = 0
    while True:
        response = downloader(
            request.url,
            timeout=config.timeout,
            max_bytes=config.max_download_bytes,
            allow_private=config.allow_private_networks,
            extra_headers=headers,
            post=body,
        )
        if not isinstance(response, HttpResponse):
            return _hide(response, key)
        retry_after = _retry_after(response.headers.get("retry-after", ""))
        used_up = response.status == 429 and _quota_used_up(response.headers)
        if response.status != 429 or used_up or attempt == RETRIES or retry_after > MAX_RETRY_WAIT:
            break
        attempt += 1
        sleep(retry_after or RETRY_SECONDS)
    if response.status != 200:
        detail = _detail(response.content)
        if used_up:  # retrying won't help, and the next search shouldn't ask either
            detail = f"{detail}; its quota is used up" if detail else "its quota is used up"
        wait = retry_after if response.status == 429 else 0.0
        return SearchApiError(api.title, response.status, detail, wait, used_up)
    try:
        data: object = json.loads(response.content)
    except ValueError:
        return SearchApiError(api.title, 200, "the answer wasn't JSON")
    found = api.parse(data)
    if found is None:
        return SearchApiError(api.title, 200, _detail(response.content) or "unexpected answer")
    return _results(found, api, page)
