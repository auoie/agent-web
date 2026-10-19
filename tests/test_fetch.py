from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_web.config import Config
from agent_web.failures import InvalidUrl, PrivateAddress, ToolMissing
from agent_web.fetch import fetch
from agent_web.model import (
    BinaryFile,
    BotCheck,
    CookiesMightHelp,
    FetchResult,
    HtmlPage,
    HttpErrorStatus,
    PdfDocument,
    PdfLayoutWarning,
    PlainText,
    RedditThread,
)
from conftest import FIXTURES, Route, Server, requires_pandoc

pytestmark = requires_pandoc


def ok(url: str, config: Config, *, refresh: bool = False) -> FetchResult:
    result = fetch(url, config, refresh=refresh)
    assert isinstance(result, FetchResult), result
    return result


def html(result: FetchResult) -> HtmlPage:
    assert isinstance(result.content, HtmlPage), result.content
    return result.content


def route_reddit_to(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    def json_url(post_id: str) -> str:
        return server.url(f"/comments/{post_id}.json")

    def thread_id(url: str) -> str:
        return "abc123"

    monkeypatch.setattr("agent_web.sites.reddit.json_url", json_url)
    monkeypatch.setattr("agent_web.sites.reddit.thread_id", thread_id)


def test_html_page(server: Server, config: Config) -> None:
    result = ok(server.url("/html"), config)
    assert isinstance(result.content, HtmlPage)
    assert result.content.title == "Test page"
    assert result.content.markdown == "# Hello\n\nSome **content** here."
    assert result.issues == ()
    assert not result.from_cache


def test_second_fetch_comes_from_cache_and_refresh_bypasses_it(
    server: Server, config: Config
) -> None:
    ok(server.url("/html"), config)
    assert ok(server.url("/html"), config).from_cache
    assert len(server.seen["/html"]) == 1
    assert not ok(server.url("/html"), config, refresh=True).from_cache
    assert len(server.seen["/html"]) == 2


def test_http_error_page(server: Server, config: Config) -> None:
    result = ok(server.url("/notfound"), config)
    assert HttpErrorStatus(404) in result.issues
    assert "Not here" in html(result).markdown


def test_bot_check_page(server: Server, config: Config) -> None:
    result = ok(server.url("/captcha"), config)
    assert BotCheck("Prove your humanity") in result.issues


def test_pdf(server: Server, config: Config) -> None:
    result = ok(server.url("/pdf"), config)
    content = result.content
    assert isinstance(content, PdfDocument)
    assert content.pages == 2
    assert "--- Page 2 ---" in content.markdown
    assert "Second page text" in content.markdown
    assert Path(content.saved_to).read_bytes().startswith(b"%PDF")
    assert PdfLayoutWarning() in result.issues


def test_text_json_binary_and_sniffing(server: Server, config: Config) -> None:
    text = ok(server.url("/text"), config).content
    assert isinstance(text, PlainText)
    assert text.text == "plain words"
    assert isinstance(ok(server.url("/json"), config).content, PlainText)
    binary = ok(server.url("/binary"), config).content
    assert isinstance(binary, BinaryFile)
    assert Path(binary.saved_to).suffix == ".png"
    assert isinstance(ok(server.url("/no-type-html"), config).content, HtmlPage)


def test_charset_and_gzip(server: Server, config: Config) -> None:
    latin = ok(server.url("/latin1"), config).content
    assert isinstance(latin, HtmlPage)
    assert latin.title == "Café"
    assert "Café crème" in latin.markdown
    assert html(ok(server.url("/gzip"), config)).markdown.startswith("# Hello")


def test_failures_are_returned(server: Server, config: Config) -> None:
    assert isinstance(fetch("ftp://example.com/", config), InvalidUrl)
    strict = replace(config, allow_private_networks=False)
    assert isinstance(fetch(server.url("/html"), strict), PrivateAddress)
    missing = ToolMissing("pandoc", "install it")
    assert fetch(server.url("/html"), config, pandoc=lambda: missing) == missing


def test_reddit_thread_via_json(
    server: Server, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.routes["/comments/abc123.json"] = Route(
        body=(FIXTURES / "reddit" / "thread.json").read_bytes(),
        headers={"Content-Type": "application/json"},
    )
    route_reddit_to(server, monkeypatch)
    result = ok(server.url("/r/x/comments/abc123/title/"), config)
    assert isinstance(result.content, RedditThread)
    assert result.content.comments == 4
    assert "u/Comfortable-Hall-188" in result.content.markdown


def test_reddit_blocked_suggests_cookies(
    server: Server, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.routes["/comments/abc123.json"] = Route(status=403, body=b"blocked")
    server.routes["/thread"] = server.routes["/captcha"]
    route_reddit_to(server, monkeypatch)
    result = ok(server.url("/thread"), config)
    assert CookiesMightHelp("reddit.com") in result.issues
    assert any(isinstance(issue, BotCheck) for issue in result.issues)
