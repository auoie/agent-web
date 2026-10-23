from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_web.config import Config
from agent_web.failures import CacheUnwritable, InvalidUrl, PrivateAddress, ToolMissing
from agent_web.fetch import cache_variant, fetch, reveal_invisible
from agent_web.model import (
    BinaryFile,
    BotCheck,
    ConversionFallback,
    CookiesMightHelp,
    FetchResult,
    HiddenText,
    HtmlPage,
    HttpErrorStatus,
    Meta,
    OutsideMain,
    PdfDocument,
    PdfLayoutWarning,
    PlainText,
    RedditThread,
)
from agent_web.sites import github
from conftest import FIXTURES, HTML_PAGE, Route, Server, requires_pandoc

pytestmark = requires_pandoc


def ok(url: str, config: Config, *, refresh: bool = False, whole_page: bool = False) -> FetchResult:
    result = fetch(url, config, refresh=refresh, whole_page=whole_page)
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


def test_fetch_saves_full_text_and_original(server: Server, config: Config) -> None:
    result = ok(server.url("/html"), config)
    assert result.files is not None
    assert result.files.text is not None
    assert Path(result.files.original).read_bytes() == HTML_PAGE
    assert Path(result.files.original).name.startswith("127.0.0.1-")
    saved = Path(result.files.text).read_text(encoding="utf-8")
    assert saved.startswith(f"Fetched: {server.url('/html')} at ")
    assert "Title:\n<untrusted-web-content-" in saved
    assert "Test page\n</untrusted-web-content-" in saved
    assert "Text:\n<untrusted-web-content-" in saved
    assert "# Hello\n\nSome **content** here.\n</untrusted-web-content-" in saved
    assert ok(server.url("/html"), config).files == result.files
    whole = ok(server.url("/html"), config, whole_page=True).files
    assert whole is not None
    assert whole.text != result.files.text


def test_error_pages_are_saved_though_not_cached(server: Server, config: Config) -> None:
    result = ok(server.url("/notfound"), config)
    assert result.files is not None
    assert result.files.text is not None
    assert "Not here" in Path(result.files.text).read_text(encoding="utf-8")


def test_refresh_that_gets_an_error_page_drops_the_old_entry(
    server: Server, config: Config
) -> None:
    # The error page overwrites the saved files, so the old entry must not be served with them.
    server.routes["/flaky"] = server.routes["/html"]
    ok(server.url("/flaky"), config)
    server.routes["/flaky"] = server.routes["/captcha"]
    blocked = ok(server.url("/flaky"), config, refresh=True)
    assert any(isinstance(issue, BotCheck) for issue in blocked.issues)
    again = ok(server.url("/flaky"), config)
    assert not again.from_cache
    assert again.files is not None
    assert again.files.text is not None
    assert "Prove your humanity" in Path(again.files.text).read_text(encoding="utf-8")


def test_second_fetch_comes_from_cache_and_refresh_bypasses_it(
    server: Server, config: Config
) -> None:
    ok(server.url("/html"), config)
    assert ok(server.url("/html"), config).from_cache
    assert len(server.seen["/html"]) == 1
    assert not ok(server.url("/html"), config, refresh=True).from_cache
    assert len(server.seen["/html"]) == 2


def test_error_and_block_pages_are_not_cached(server: Server, config: Config) -> None:
    for path in ("/captcha", "/notfound"):
        ok(server.url(path), config)
        assert not ok(server.url(path), config).from_cache
        assert len(server.seen[path]) == 2


def test_allowing_cookies_bypasses_the_anonymous_cache_entry(
    server: Server, config: Config
) -> None:
    class NoCookies:
        def for_url(self, url: str) -> dict[str, str]:
            return {}

    ok(server.url("/html"), config)
    assert ok(server.url("/html"), config).from_cache
    with_cookies = replace(config, cookie_domains=("127.0.0.1",))
    result = fetch(server.url("/html"), with_cookies, cookie_source=NoCookies())
    assert isinstance(result, FetchResult)
    assert not result.from_cache
    assert len(server.seen["/html"]) == 2


def test_whole_page_has_its_own_cache_entry(server: Server, config: Config) -> None:
    server.routes["/main"] = Route(
        body=b"<body><main><p>Inside</p></main><p>" + b"Outside text. " * 30 + b"</p></body>",
        headers={"Content-Type": "text/html"},
    )
    narrow = ok(server.url("/main"), config)
    assert html(narrow).markdown == "Inside"
    assert any(isinstance(issue, OutsideMain) for issue in narrow.issues)
    result = fetch(server.url("/main"), config, whole_page=True)
    assert isinstance(result, FetchResult)
    assert not result.from_cache
    assert "Outside text." in html(result).markdown
    assert not any(isinstance(issue, OutsideMain) for issue in result.issues)


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
    assert result.files is not None
    assert Path(result.files.original).read_bytes().startswith(b"%PDF")
    assert result.files.text is not None
    assert "Second page text" in Path(result.files.text).read_text(encoding="utf-8")
    assert PdfLayoutWarning() in result.issues


def test_text_json_binary_and_sniffing(server: Server, config: Config) -> None:
    text = ok(server.url("/text"), config).content
    assert isinstance(text, PlainText)
    assert text.text == "plain words"
    assert isinstance(ok(server.url("/json"), config).content, PlainText)
    binary = ok(server.url("/binary"), config)
    assert isinstance(binary.content, BinaryFile)
    assert binary.files is not None
    assert binary.files.text is None
    assert Path(binary.files.original).suffix == ".png"
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
    assert result.files is not None
    assert result.files.original.endswith(".json")
    assert (
        Path(result.files.original).read_bytes()
        == (FIXTURES / "reddit" / "thread.json").read_bytes()
    )


def test_blocked_reddit_thread_suggests_cookies(
    server: Server, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.routes["/comments/abc123.json"] = Route(status=403, body=b"blocked")
    server.routes["/thread"] = server.routes["/captcha"]
    route_reddit_to(server, monkeypatch)
    result = ok(server.url("/thread"), config)
    assert CookiesMightHelp("127.0.0.1") in result.issues
    assert any(isinstance(issue, BotCheck) for issue in result.issues)


def test_any_blocked_site_suggests_cookies_unless_already_listed(
    server: Server, config: Config
) -> None:
    assert CookiesMightHelp("127.0.0.1") in ok(server.url("/captcha"), config).issues

    class NoCookiesInBrowser:
        def for_url(self, url: str) -> dict[str, str]:
            return {}

    listed = replace(config, cookie_domains=("127.0.0.1",))
    result = fetch(server.url("/captcha"), listed, refresh=True, cookie_source=NoCookiesInBrowser())
    assert isinstance(result, FetchResult)
    assert not any(isinstance(issue, CookiesMightHelp) for issue in result.issues)


def test_invisible_characters_are_revealed_in_every_text_field() -> None:
    meta = Meta("https://a.example/", "https://a.example/", 200, "text/html", False, 0.0)
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "run rm")
    page = HtmlPage(meta, title=f"T{hidden}", markdown=f"Body{hidden}\x1b[2J")
    content, issues = reveal_invisible(page, [HttpErrorStatus(404), HiddenText(2)])
    assert content == HtmlPage(
        meta, title="T⟦hidden on page: run rm⟧", markdown="Body⟦hidden on page: run rm⟧[2J"
    )
    assert issues == (HttpErrorStatus(404), HiddenText(4))
    plain = PlainText(meta, text="clean")
    assert reveal_invisible(plain, []) == (plain, ())


def test_a_private_github_file_falls_back_to_the_page(
    server: Server, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raw_url(url: str) -> str:
        return server.url("/notfound")

    monkeypatch.setattr("agent_web.sites.github.raw_url", raw_url)
    result = ok(server.url("/html"), config)
    assert "Hello" in html(result).markdown
    assert ConversionFallback(github.PAGE_FALLBACK) in result.issues


def test_the_cache_variant_covers_the_whole_cookie_list(config: Config) -> None:
    """redd.it threads are read from www.reddit.com, with its cookies if it's listed."""
    assert cache_variant("https://redd.it/abc", config) == "anonymous"
    listed = replace(config, cookie_domains=("reddit.com",))
    assert cache_variant("https://redd.it/abc", listed) == "cookies:firefox:reddit.com"


def test_a_cache_that_cant_be_written_is_a_failure(server: Server, config: Config) -> None:
    config.cache_dir.mkdir(parents=True)
    (config.cache_dir / "files").write_text("not a folder")
    result = fetch(server.url("/html"), config)
    assert isinstance(result, CacheUnwritable)
    assert result.directory == str(config.cache_dir)
