"""Google search with the user's logged-in browser cookies.

Google serves anonymous non-browser clients an "enable JavaScript" page, but answers requests that
carry a signed-in session with a normal results page. Results are parsed structurally (a link
containing an <h3> is a result) rather than by Google's obfuscated class names.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Literal
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
    SEARCH_PAUSE_MINUTES,
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    JavaScriptWall,
    NotSignedIn,
    SearchPaused,
)
from agent_web.http import DownloadFailure, HttpResponse, download
from agent_web.invisible import reveal
from agent_web.search.limiter import Paused, RateLimiter, limiter_for
from agent_web.search.refresh import Refresher, refresher_for
from agent_web.urls import domain_matches, normalize

SEARCH_URL = "https://www.google.com/search"
SERVICE = "Google"
AUTH_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID"})
RESULTS_PER_PAGE = 10
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
    page: int = 1


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
    for node in list(doc.iter("script", "style", "noscript")):
        if isinstance(node, lxml.html.HtmlElement):
            node.drop_tree()  # keeps the text that follows the element
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
        snippet = _snippet(_container(link), link)
        results.append(SearchResult(title=reveal(title)[0], url=url, snippet=reveal(snippet)[0]))
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
    # Every results page, including a genuinely empty one, carries a hidden "enable JavaScript"
    # fallback, so an empty page has to be recognized before that check. Searches use hl=en.
    if "did not match any documents" in lowered:
        return None
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


def search_url(query: str, page: int) -> str:
    params: dict[str, str | int] = {"q": query, "hl": "en"}
    if page:
        params["start"] = page * RESULTS_PER_PAGE
    return f"{SEARCH_URL}?{urlencode(params)}"


def overlay(base: list[Cookie], extra: list[Cookie]) -> list[Cookie]:
    """`base` with each cookie that `extra` also has replaced by the one from `extra`."""
    replaced = {(c.domain, c.name, c.path) for c in extra}
    return [c for c in base if (c.domain, c.name, c.path) not in replaced] + extra


def _paused(paused: Paused) -> SearchPaused:
    return SearchPaused(SERVICE, paused.cause, math.ceil(paused.seconds_left / 60))


def _attempt(
    url: str,
    config: Config,
    cookies: list[Cookie],
    downloader: Downloader,
    limiter: RateLimiter,
) -> list[SearchResult] | Failure:
    """One request to Google, in turn with every other process."""
    if (paused := limiter.wait()) is not None:
        return _paused(paused)
    response = downloader(
        url,
        timeout=config.timeout,
        max_bytes=config.max_download_bytes,
        cookie_source=GoogleCookies(cookies),
        impersonate_with_cookies=impersonation_for(config.browser),
    )
    if not isinstance(response, HttpResponse):
        return response
    html_text = decode_html(response.content, response.content_type)
    found = parse_results(html_text)
    problem = classify_response(response, html_text, found)
    return found if problem is None else problem


def search(
    query: str,
    page: int,
    config: Config,
    *,
    cookie_loader: CookieLoader = load_browser_cookies,
    downloader: Downloader = download,
    limiter: RateLimiter | None = None,
    refresher: Refresher | Literal["auto"] | None = "auto",
) -> SearchResults | Failure:
    """The results on one Google results page (1 is the first): one request.

    If Google answers with its JavaScript check, a headless browser runs the check (refresh.py)
    and the search is retried once. A captcha, or a check the refresh doesn't get past, pauses
    every process's searches for SEARCH_PAUSE_MINUTES.
    """
    cookies = cookie_loader(config.browser, "google.com", config.cookie_file)
    if isinstance(cookies, CookieReadFailure):
        return cookies
    if not signed_in(cookies):
        return NotSignedIn(SERVICE, config.browser)
    limiter = limiter or limiter_for(config)
    if refresher == "auto":
        refresher = refresher_for(config)
    if refresher is not None:
        cookies = overlay(cookies, refresher.cookies())
    url = search_url(query, page - 1)
    outcome = _attempt(url, config, cookies, downloader, limiter)
    if isinstance(outcome, JavaScriptWall) and refresher is not None:
        refreshed = refresher.refresh(url, limiter)
        if isinstance(refreshed, Paused):
            return _paused(refreshed)
        if isinstance(refreshed, str):
            refresher.log(f"refresh failed: {refreshed}")
        else:
            outcome = _attempt(
                url, config, overlay(cookies, refreshed.cookies), downloader, limiter
            )
            result = "got results" if isinstance(outcome, list) else f"got {outcome}"
            refresher.log(f"{refreshed.how}; the retry {result}")
    if isinstance(outcome, Captcha | JavaScriptWall):
        cause = "a captcha" if isinstance(outcome, Captcha) else "its JavaScript check"
        limiter.pause(cause, SEARCH_PAUSE_MINUTES * 60)
    if isinstance(outcome, list):
        return SearchResults(query, tuple(outcome), page)
    return outcome
