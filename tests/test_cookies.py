from __future__ import annotations

import time
from http.cookiejar import Cookie
from pathlib import Path

from agent_web.cookies import AllowlistCookieSource, cookies_for_url, impersonation_for
from agent_web.failures import CookieReadFailure


def cookie(
    name: str,
    value: str,
    domain: str,
    path: str = "/",
    secure: bool = False,
    expires: int | None = None,
) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )


def test_domain_and_subdomain_matching() -> None:
    jar = [
        cookie("a", "1", ".reddit.com"),
        cookie("b", "2", "www.reddit.com"),
        cookie("c", "3", "notreddit.com"),
        cookie("d", "4", "old.reddit.com"),
    ]
    assert cookies_for_url(jar, "https://www.reddit.com/r/x") == {"a": "1", "b": "2"}
    assert cookies_for_url(jar, "https://reddit.com/") == {"a": "1"}


def test_path_secure_and_expiry() -> None:
    now = time.time()
    jar = [
        cookie("p", "api", "example.com", path="/api"),
        cookie("s", "secret", "example.com", secure=True),
        cookie("old", "x", "example.com", expires=int(now) - 10),
        cookie("new", "y", "example.com", expires=int(now) + 1000),
    ]
    assert cookies_for_url(jar, "https://example.com/api/v1", now) == {
        "p": "api",
        "s": "secret",
        "new": "y",
    }
    assert cookies_for_url(jar, "https://example.com/apis", now) == {"s": "secret", "new": "y"}
    assert cookies_for_url(jar, "http://example.com/api", now) == {"p": "api", "new": "y"}


def test_most_specific_cookie_wins() -> None:
    jar = [
        cookie("id", "domain-wide", ".example.com"),
        cookie("id", "host", "www.example.com"),
        cookie("id", "path", ".example.com", path="/app"),
    ]
    assert cookies_for_url(jar, "https://www.example.com/app/x") == {"id": "path"}
    assert cookies_for_url(jar, "https://www.example.com/") == {"id": "host"}


def test_allowlist_only_sends_to_listed_domains() -> None:
    calls: list[str] = []

    def loader(browser: str, domain: str, cookie_file: Path | None) -> list[Cookie]:
        calls.append(domain)
        return [cookie("session", "s3cret", ".reddit.com"), cookie("x", "1", ".evil.io")]

    source = AllowlistCookieSource("firefox", ["reddit.com"], loader=loader)
    assert source.for_url("https://evil.io/") == {}
    assert source.for_url("https://www.reddit.com/r/a") == {"session": "s3cret"}
    assert source.for_url("https://old.reddit.com/") == {"session": "s3cret"}
    assert calls == ["reddit.com"]  # loaded once, cached


def test_allowlist_propagates_read_failure() -> None:
    def loader(browser: str, domain: str, cookie_file: Path | None) -> CookieReadFailure:
        return CookieReadFailure(browser, "locked")

    source = AllowlistCookieSource("chrome", ["reddit.com"], loader=loader)
    assert source.for_url("https://reddit.com/") == CookieReadFailure("chrome", "locked")
    assert source.for_url("https://example.com/") == {}


def test_impersonation_matches_cookie_browser() -> None:
    assert impersonation_for("firefox") == "firefox"
    assert impersonation_for("librewolf") == "firefox"
    assert impersonation_for("chrome") == "chrome"
    assert impersonation_for("brave") == "chrome"
