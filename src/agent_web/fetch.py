"""The `web fetch` pipeline: URL → download → convert → detect issues → cache."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from agent_web import detect
from agent_web.cache import PageCache
from agent_web.config import Config
from agent_web.convert.html import convert_html, decode_html
from agent_web.convert.pandoc import Pandoc, find_pandoc
from agent_web.convert.pdf import PdfUnreadable, convert_pdf
from agent_web.cookies import AllowlistCookieSource, CookieSource, impersonation_for
from agent_web.failures import Failure, InvalidUrl, ToolMissing
from agent_web.http import HttpResponse, download
from agent_web.model import (
    BinaryFile,
    BotCheck,
    Content,
    ConversionFallback,
    CookiesMightHelp,
    FetchResult,
    HtmlPage,
    HttpErrorStatus,
    Issue,
    Meta,
    NearlyEmpty,
    PdfDocument,
    PlainText,
    RedditThread,
)
from agent_web.sites import reddit
from agent_web.urls import domain_matches, hostname, validate_url

Kind = Literal["html", "pdf", "text", "binary"]
_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
    }
)


def classify(response: HttpResponse) -> Kind:
    mime = response.content_type.split(";")[0].strip().lower()
    head = response.content[:1024].lstrip().lower()
    if mime == "application/pdf" or response.content.startswith(b"%PDF-"):
        return "pdf"
    if mime in ("text/html", "application/xhtml+xml"):
        return "html"
    if (not mime or mime == "application/octet-stream") and head.startswith(
        (b"<!doctype html", b"<html")
    ):
        return "html"
    if mime.startswith("text/") or mime in _TEXT_TYPES or mime.endswith(("+json", "+xml")):
        return "text"
    if not mime and b"\x00" not in response.content[:4096]:
        return "text"
    return "binary"


def _meta(url: str, response: HttpResponse, now: float) -> Meta:
    return Meta(
        url=url,
        final_url=response.url,
        status=response.status,
        content_type=response.content_type,
        cookies_sent=response.cookies_sent,
        fetched_at=now,
    )


def _convert(
    url: str,
    response: HttpResponse,
    cache: PageCache,
    pandoc: Callable[[], Pandoc | ToolMissing],
    now: float,
    whole_page: bool = False,
) -> tuple[Content, list[Issue]] | ToolMissing:
    meta = _meta(url, response, now)
    match classify(response):
        case "pdf":
            saved = cache.save_file(url, response.content, ".pdf")
            converted = convert_pdf(response.content)
            if isinstance(converted, PdfUnreadable):
                issues: list[Issue] = [ConversionFallback(f"PDF unreadable: {converted.reason}")]
                if response.status >= 400:
                    issues.insert(0, HttpErrorStatus(response.status))
                return PdfDocument(meta, markdown="", pages=0, saved_to=str(saved)), issues
            content = PdfDocument(
                meta, markdown=converted.markdown, pages=converted.pages, saved_to=str(saved)
            )
            return content, detect.pdf_issues(
                status=response.status, pages=converted.pages, text_chars=converted.text_chars
            )
        case "html":
            found = pandoc()
            if isinstance(found, ToolMissing):
                return found
            page = convert_html(
                response.content, response.content_type, response.url, found, whole_page
            )
            issues = detect.html_issues(
                status=response.status,
                title=page.title,
                markdown=page.markdown,
                text_chars=page.text_chars,
                html_chars=page.html_chars,
                hidden_count=page.hidden_count,
                outside_main_chars=page.outside_main_chars,
            )
            if page.fallback_reason:
                issues.append(ConversionFallback(page.fallback_reason))
            return HtmlPage(meta, title=page.title, markdown=page.markdown), issues
        case "text":
            text = decode_html(response.content, response.content_type)
            issues = [HttpErrorStatus(response.status)] if response.status >= 400 else []
            return PlainText(meta, text=text), issues
        case "binary":
            suffix = "." + (response.content_type.split(";")[0].split("/")[-1] or "bin")[:10]
            saved = cache.save_file(url, response.content, suffix)
            issues = [HttpErrorStatus(response.status)] if response.status >= 400 else []
            return BinaryFile(meta, saved_to=str(saved), size=len(response.content)), issues


def _try_reddit(
    url: str, post_id: str, config: Config, source: CookieSource, now: float
) -> RedditThread | None:
    response = download(
        reddit.json_url(post_id),
        timeout=config.timeout,
        max_bytes=config.max_download_bytes,
        allow_private=config.allow_private_networks,
        cookie_source=source,
        impersonate_with_cookies=impersonation_for(config.browser),
    )
    if not isinstance(response, HttpResponse) or response.status != 200:
        return None
    rendered = reddit.parse_and_render(response.content)
    if rendered is None:
        return None
    return RedditThread(
        _meta(url, response, now),
        title=rendered.title,
        markdown=rendered.markdown,
        comments=rendered.comments,
        not_loaded=rendered.not_loaded,
    )


def fetch(
    url: str,
    config: Config,
    *,
    refresh: bool = False,
    whole_page: bool = False,
    cookie_source: CookieSource | None = None,
    pandoc: Callable[[], Pandoc | ToolMissing] | None = None,
    now: Callable[[], float] = time.time,
) -> FetchResult | Failure:
    valid = validate_url(url)
    if isinstance(valid, InvalidUrl):
        return valid
    cache = PageCache(config.cache_dir, config.cache_ttl, config.cache_max_bytes)
    variant = cache_variant(valid, config) + (" whole-page" if whole_page else "")
    if not refresh and (cached := cache.load(valid, variant)) is not None:
        return cached

    source = cookie_source or AllowlistCookieSource(
        config.browser, config.cookie_domains, config.cookie_file
    )
    find = pandoc or (lambda: find_pandoc(config.pandoc))
    issues: list[Issue] = []

    if (post_id := reddit.thread_id(valid)) is not None:
        thread = _try_reddit(valid, post_id, config, source, now())
        if thread is not None:
            result = FetchResult(thread, ())
            cache.store(valid, result, variant)
            return result

    response = download(
        valid,
        timeout=config.timeout,
        max_bytes=config.max_download_bytes,
        allow_private=config.allow_private_networks,
        cookie_source=source,
        impersonate_with_cookies=impersonation_for(config.browser),
    )
    if not isinstance(response, HttpResponse):
        return response
    converted = _convert(valid, response, cache, find, now(), whole_page)
    if isinstance(converted, ToolMissing):
        return converted
    content, found_issues = converted
    issues.extend(found_issues)

    if post_id is not None and not response.cookies_sent and _looks_blocked(issues):
        issues.append(CookiesMightHelp(reddit.COOKIE_DOMAIN))

    result = FetchResult(content, tuple(issues))
    if cacheable(result):
        cache.store(valid, result, variant)
    return result


def cache_variant(url: str, config: Config) -> str:
    """What besides the URL decides the result: whether (and from which browser) cookies are sent.
    Changing `fetch.cookie_domains` or `cookies.browser` therefore never serves an old result."""
    host = hostname(url)
    if any(domain_matches(host, domain) for domain in config.cookie_domains):
        return f"cookies:{config.browser}"
    return "anonymous"


def cacheable(result: FetchResult) -> bool:
    """Error, block and nearly empty pages aren't cached: they're often temporary or fixed by a
    config change, and cheap to fetch again."""
    return not any(
        isinstance(issue, BotCheck | HttpErrorStatus | NearlyEmpty) for issue in result.issues
    )


def _looks_blocked(issues: list[Issue]) -> bool:
    return any(
        isinstance(issue, BotCheck)
        or (isinstance(issue, HttpErrorStatus) and issue.status in (401, 403, 429))
        for issue in issues
    )
