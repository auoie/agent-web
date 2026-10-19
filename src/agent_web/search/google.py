"""Google search with the user's logged-in browser cookies.

Google serves anonymous non-browser clients an "enable JavaScript" page, but answers requests that
carry a signed-in session with a normal results page. Results are parsed structurally (a link
containing an <h3> is a result) rather than by Google's obfuscated class names.
"""

from __future__ import annotations

import fcntl
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import lxml.html

from agent_web.config import Config
from agent_web.convert.html import decode_html, parse_document
from agent_web.cookies import (
    CookieLoader,
    cookies_for_url,
    impersonation_for,
    load_browser_cookies,
)
from agent_web.failures import (
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    JavaScriptWall,
    NotSignedIn,
)
from agent_web.http import DownloadFailure, HttpResponse, download
from agent_web.urls import domain_matches, normalize

SEARCH_URL = "https://www.google.com/search"
SERVICE = "Google"
AUTH_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID"})
RESULTS_PER_PAGE = 10
MAX_PAGES = 5
MAX_SNIPPET = 400
_GOOGLE_HOST = re.compile(r"(?:^|\.)google\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_INTERNAL_PATHS = (
    "/search",
    "/url",
    "/imgres",
    "/preferences",
    "/setprefs",
    "/advanced_search",
    "/sorry",
    "/webhp",
    "/httpservice",
)
_BLOCK_TAGS = frozenset(
    {"div", "p", "br", "li", "ul", "ol", "table", "tr", "td", "th", "section", "article"}
    | {f"h{i}" for i in range(1, 7)}
)

Downloader = Callable[..., HttpResponse | DownloadFailure]


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchResults:
    query: str
    results: tuple[SearchResult, ...]
    stopped_early: Failure | None = None


def _text(
    node: lxml.html.HtmlElement, skip: Callable[[lxml.html.HtmlElement], bool] | None = None
) -> str:
    """Element text with spaces at block boundaries; subtrees where `skip` is true are left out."""
    parts: list[str] = []

    def walk(element: lxml.html.HtmlElement) -> None:
        block = element.tag in _BLOCK_TAGS
        if block:
            parts.append(" ")
        if element.text:
            parts.append(element.text)
        for child in element:
            if isinstance(child, lxml.html.HtmlElement) and not (skip and skip(child)):
                walk(child)
            if child.tail:
                parts.append(child.tail)
        if block:
            parts.append(" ")

    walk(node)
    return " ".join("".join(parts).split())


def _unwrap(href: str) -> str:
    """Google redirect links (/url?q=…) point at the real target; return that instead."""
    absolute = urljoin(SEARCH_URL, href)
    parts = urlsplit(absolute)
    if _GOOGLE_HOST.search(parts.hostname or "") and parts.path == "/url":
        query = parse_qs(parts.query)
        for key in ("q", "url"):
            if query.get(key):
                return query[key][0]
    return absolute


def _is_result_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if _GOOGLE_HOST.search(host) and parts.path.startswith(_INTERNAL_PATHS):
        return False
    return not host.endswith("googleusercontent.com")


def _container(link: lxml.html.HtmlElement) -> lxml.html.HtmlElement:
    """The largest ancestor of the result link that still holds only this one <h3>."""
    container = link
    for _ in range(10):
        parent = container.getparent()
        if not isinstance(parent, lxml.html.HtmlElement):
            break
        if sum(1 for _ in parent.iter("h3")) > 1:
            break
        container = parent
    return container


def _snippet(container: lxml.html.HtmlElement, link: lxml.html.HtmlElement) -> str:
    def is_label(element: lxml.html.HtmlElement) -> bool:
        return element.tag in ("cite", "h2", "h3")

    # Text inside the link besides the title and URL breadcrumb: the site name and metadata,
    # which Google repeats next to the snippet.
    site = _text(link, is_label)
    snippet = _text(container, lambda e: e is link or is_label(e))
    if site and snippet.startswith(site):
        snippet = snippet[len(site) :].lstrip(" \u00b7-\u2013\u2014")
    if len(snippet) > MAX_SNIPPET:
        snippet = snippet[:MAX_SNIPPET].rsplit(" ", 1)[0] + " …"
    return snippet


def parse_results(html_text: str) -> list[SearchResult]:
    doc = parse_document(html_text)
    if doc is None:
        return []
    results: list[SearchResult] = []
    seen: set[str] = set()
    for link in doc.iter("a"):
        if not isinstance(link, lxml.html.HtmlElement):
            continue
        heading = next(iter(link.iter("h3")), None)
        href = link.get("href")
        if not isinstance(heading, lxml.html.HtmlElement) or not href:
            continue
        url = _unwrap(href)
        key = normalize(url)
        if not _is_result_url(url) or key in seen:
            continue
        title = _text(heading)
        if not title:
            continue
        seen.add(key)
        results.append(SearchResult(title=title, url=url, snippet=_snippet(_container(link), link)))
    return results


def classify_response(
    response: HttpResponse, html_text: str, results: list[SearchResult]
) -> Captcha | ConsentRequired | JavaScriptWall | None:
    """Why a Google response isn't a usable results page, or None if it is."""
    urls = (response.url, *response.redirects)
    if any("/sorry/" in url for url in urls):
        return Captcha(SERVICE)
    if any((urlsplit(url).hostname or "").startswith("consent.google.") for url in urls):
        return ConsentRequired(SERVICE)
    if results:
        return None
    lowered = html_text.lower()
    if "unusual traffic" in lowered or "captcha-form" in lowered:
        return Captcha(SERVICE)
    if "enablejs" in lowered or "please click here if you are not redirected" in lowered:
        return JavaScriptWall(SERVICE)
    return None


class GoogleCookies:
    """A CookieSource that sends the loaded Google cookies to Google hosts only."""

    def __init__(self, cookies: Iterable[Cookie]) -> None:
        self.cookies = list(cookies)

    def for_url(self, url: str) -> dict[str, str]:
        host = (urlsplit(url).hostname or "").lower()
        return cookies_for_url(self.cookies, url) if domain_matches(host, "google.com") else {}


def signed_in(cookies: Iterable[Cookie]) -> bool:
    return any(
        c.name in AUTH_COOKIES and domain_matches("www.google.com", c.domain.lstrip("."))
        for c in cookies
    )


class RateLimiter:
    """Keeps at least `min_interval` seconds between requests, across processes (flock)."""

    def __init__(
        self,
        path: Path,
        min_interval: float,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = path
        self.min_interval = min_interval
        self._now = now
        self._sleep = sleep

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            try:
                last = float(handle.read().strip() or 0)
            except ValueError:
                last = 0.0
            delay = last + self.min_interval - self._now()
            if delay > 0:
                self._sleep(min(delay, self.min_interval))
            handle.seek(0)
            handle.truncate()
            handle.write(f"{self._now():.3f}")


def search_url(query: str, page: int) -> str:
    params: dict[str, str | int] = {"q": query, "hl": "en"}
    if page:
        params["start"] = page * RESULTS_PER_PAGE
    return f"{SEARCH_URL}?{urlencode(params)}"


def search(
    query: str,
    n: int,
    config: Config,
    *,
    cookie_loader: CookieLoader = load_browser_cookies,
    downloader: Downloader = download,
    limiter: RateLimiter | None = None,
) -> SearchResults | Failure:
    cookies = cookie_loader(config.browser, "google.com", config.cookie_file)
    if isinstance(cookies, CookieReadFailure):
        return cookies
    if not signed_in(cookies):
        return NotSignedIn(SERVICE, config.browser)
    source = GoogleCookies(cookies)
    limiter = limiter or RateLimiter(
        config.cache_dir / "google-last-request", config.search_min_interval
    )
    results: list[SearchResult] = []
    seen: set[str] = set()
    pages = min(MAX_PAGES, math.ceil(n / RESULTS_PER_PAGE) + 1)
    for page in range(pages):
        limiter.wait()
        response = downloader(
            search_url(query, page),
            timeout=config.timeout,
            max_bytes=config.max_download_bytes,
            cookie_source=source,
            impersonate_with_cookies=impersonation_for(config.browser),
        )
        if not isinstance(response, HttpResponse):
            return SearchResults(query, tuple(results), response) if results else response
        html_text = decode_html(response.content, response.content_type)
        found = parse_results(html_text)
        if (problem := classify_response(response, html_text, found)) is not None:
            return SearchResults(query, tuple(results), problem) if results else problem
        new = [r for r in found if normalize(r.url) not in seen]
        if not new:
            break
        for result in new:
            seen.add(normalize(result.url))
        results.extend(new)
        if len(results) >= n:
            break
    return SearchResults(query, tuple(results[:n]))
