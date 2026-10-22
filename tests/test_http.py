from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

import pytest

from agent_web.cookies import CookieSource
from agent_web.failures import (
    CookieReadFailure,
    InvalidUrl,
    NetworkFailure,
    PrivateAddress,
    TooLarge,
    TooManyRedirects,
)
from agent_web.http import HttpResponse, download, resolve_pin
from conftest import HTML_PAGE, Server

MB = 1024 * 1024


class OnlyHost:
    """Cookie source that gives a cookie to one host only."""

    def __init__(self, host: str) -> None:
        self.host = host

    def for_url(self, url: str) -> dict[str, str]:
        return {"session": "s3cret"} if urlsplit(url).hostname == self.host else {}


def get(url: str, cookie_source: CookieSource | None = None) -> HttpResponse:
    result = download(url, timeout=5, max_bytes=MB, allow_private=True, cookie_source=cookie_source)
    assert isinstance(result, HttpResponse), result
    return result


def test_basic_download(server: Server) -> None:
    response = get(server.url("/html"))
    assert response.status == 200
    assert response.content == HTML_PAGE
    assert response.content_type.startswith("text/html")
    assert response.redirects == ()
    assert not response.cookies_sent


def test_follows_same_host_redirect(server: Server) -> None:
    response = get(server.url("/redirect-same"))
    assert response.url == server.url("/html")
    assert response.redirects == (server.url("/html"),)
    assert response.content == HTML_PAGE


def test_cookies_only_for_the_allowed_host(server: Server) -> None:
    direct = get(server.url("/echo-cookies"), cookie_source=OnlyHost("127.0.0.1"))
    assert direct.content == b"cookies: session=s3cret"
    assert direct.cookies_sent


def test_redirect_never_carries_cookies_to_another_host(server: Server) -> None:
    response = get(server.url("/redirect-cross"), cookie_source=OnlyHost("127.0.0.1"))
    assert urlsplit(response.url).hostname == "localhost"
    assert response.content == b"cookies: none"
    first_hop = server.seen["/redirect-cross"][0]
    assert first_hop.get("Cookie") == "session=s3cret"


def test_redirect_never_carries_extra_headers_to_another_host(server: Server) -> None:
    key = {"X-Subscription-Token": "s3cret"}
    result = download(
        server.url("/redirect-cross"),
        timeout=5,
        max_bytes=MB,
        allow_private=True,
        extra_headers=key,
    )
    assert isinstance(result, HttpResponse), result
    assert server.seen["/redirect-cross"][0].get("X-Subscription-Token") == "s3cret"
    assert "s3cret" not in str(server.seen["/echo-cookies"][0])


def test_redirect_loop(server: Server) -> None:
    result = download(server.url("/loop"), timeout=5, max_bytes=MB, allow_private=True)
    assert isinstance(result, TooManyRedirects)


def test_redirect_to_non_http_scheme(server: Server) -> None:
    result = download(server.url("/redirect-ftp"), timeout=5, max_bytes=MB, allow_private=True)
    assert isinstance(result, InvalidUrl)


def test_declared_size_over_limit(server: Server) -> None:
    result = download(server.url("/big"), timeout=5, max_bytes=MB, allow_private=True)
    assert isinstance(result, TooLarge)


def test_streamed_size_over_limit(server: Server) -> None:
    result = download(server.url("/big-chunked"), timeout=5, max_bytes=MB, allow_private=True)
    assert isinstance(result, TooLarge)


def test_timeout(server: Server) -> None:
    result = download(server.url("/slow"), timeout=1, max_bytes=MB, allow_private=True)
    assert isinstance(result, NetworkFailure)


def test_connection_refused() -> None:
    result = download("http://127.0.0.1:9/", timeout=3, max_bytes=MB, allow_private=True)
    assert isinstance(result, NetworkFailure)


def test_private_address_blocked_by_default(server: Server) -> None:
    result = download(server.url("/html"), timeout=5, max_bytes=MB)
    assert result == PrivateAddress("127.0.0.1")
    assert "/html" not in server.seen


def test_redirect_into_private_network_blocked(server: Server) -> None:
    # Allowed for the first hop only through allow_private; a public→private redirect is the
    # classic SSRF trick, so each hop is checked again.
    result = download(server.url("/redirect-cross"), timeout=5, max_bytes=MB, allow_private=False)
    assert isinstance(result, PrivateAddress)


def test_connection_is_pinned_to_the_checked_address(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `pinned.invalid` doesn't exist in DNS, so reaching the test server proves curl connected to
    # the address that was checked rather than looking the name up again (DNS rebinding).
    real = socket.getaddrinfo

    def resolve(host: str, port: int | None, **kwargs: int) -> object:
        return real("127.0.0.1" if host == "pinned.invalid" else host, port, **kwargs)

    def anything_is_public(address: str) -> bool:
        return True

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr("agent_web.urls.is_public_ip", anything_is_public)
    url = server.url("/html").replace("127.0.0.1", "pinned.invalid")
    result = download(url, timeout=5, max_bytes=MB)
    assert isinstance(result, HttpResponse), result
    assert result.content == HTML_PAGE


def test_resolve_pin() -> None:
    assert resolve_pin("https://a.example/x", ("1.2.3.4", "2001:db8::1")) == (
        "a.example:443:1.2.3.4,[2001:db8::1]"
    )
    assert resolve_pin("http://a.example:8080/", ("1.2.3.4",)) == "a.example:8080:1.2.3.4"
    assert resolve_pin("http://93.184.215.14/", ("93.184.215.14",)) is None


def test_invalid_url() -> None:
    assert isinstance(download("file:///etc/passwd", timeout=1, max_bytes=MB), InvalidUrl)


def test_gzip_is_decoded(server: Server) -> None:
    assert get(server.url("/gzip")).content == HTML_PAGE


def test_cookie_read_failure_is_returned(server: Server) -> None:
    class Broken:
        def for_url(self, url: str) -> CookieReadFailure:
            return CookieReadFailure("firefox", "database locked")

    result = download(
        server.url("/html"), timeout=5, max_bytes=MB, allow_private=True, cookie_source=Broken()
    )
    assert result == CookieReadFailure("firefox", "database locked")


def test_post_sends_the_body_and_isnt_redirected(server: Server) -> None:
    from conftest import Route

    def echo(handler: BaseHTTPRequestHandler) -> Route:
        return Route(body=b"got it", headers={"Content-Type": "text/plain"})

    server.routes["/api"] = echo
    response = download(
        server.url("/api"),
        timeout=5,
        max_bytes=1000,
        allow_private=True,
        post=b'{"q": "x"}',
        extra_headers={"Content-Type": "application/json"},
    )
    assert isinstance(response, HttpResponse)
    assert response.content == b"got it"
    assert server.seen["/api"][0]["Content-Type"] == "application/json"
    assert server.seen["/api"][0]["_body"] == '{"q": "x"}'
    moved = download(
        server.url("/redirect-same"), timeout=5, max_bytes=1000, allow_private=True, post=b"{}"
    )
    assert isinstance(moved, HttpResponse)
    assert moved.status == 302  # not followed
