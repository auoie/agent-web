"""HTTP downloads with browser impersonation, per-hop cookie scoping and hard limits."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin, urlsplit

from agent_web.cookies import CookieSource
from agent_web.failures import (
    CookieReadFailure,
    InvalidUrl,
    NetworkFailure,
    PrivateAddress,
    TooLarge,
    TooManyRedirects,
)
from agent_web.urls import public_addresses, validate_url

if TYPE_CHECKING:
    from curl_cffi.requests import Response

MAX_REDIRECTS = 10
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

DownloadFailure = (
    InvalidUrl | PrivateAddress | NetworkFailure | TooLarge | TooManyRedirects | CookieReadFailure
)


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    reason: str
    headers: dict[str, str]
    content: bytes
    redirects: tuple[str, ...] = ()
    cookies_sent: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass(frozen=True)
class _Oversized:
    """Internal marker: the body went over the limit while streaming."""


def download(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    allow_private: bool = False,
    cookie_source: CookieSource | None = None,
    impersonate_with_cookies: str = "chrome",
    impersonate_anonymous: str = "chrome",
    extra_headers: dict[str, str] | None = None,
) -> HttpResponse | DownloadFailure:
    """GET `url`, following redirects ourselves so every hop gets its own checks.

    Each hop is validated (scheme, credentials, private addresses, with the connection pinned to
    the checked addresses) and gets cookies only if `cookie_source` returns some for that exact
    URL, so a redirect never carries cookies to another site. Bodies larger than `max_bytes` are
    refused.
    """
    from curl_cffi import CurlOpt
    from curl_cffi import requests as curl_requests

    current = validate_url(url)
    if isinstance(current, InvalidUrl):
        return current
    redirects: list[str] = []
    cookies_sent = False
    for _ in range(MAX_REDIRECTS + 1):
        options: dict[CurlOpt, Any] = {}
        if not allow_private:
            addresses = public_addresses(current)
            if isinstance(addresses, PrivateAddress | NetworkFailure):
                return addresses
            if pin := resolve_pin(current, addresses):
                options[CurlOpt.RESOLVE] = [pin]
        cookies: dict[str, str] = {}
        if cookie_source is not None:
            found = cookie_source.for_url(current)
            if isinstance(found, CookieReadFailure):
                return found
            cookies = found
        cookies_sent = cookies_sent or bool(cookies)
        target = impersonate_with_cookies if cookies else impersonate_anonymous
        with curl_requests.Session[curl_requests.Response](curl_options=options) as session:
            try:
                response = session.get(
                    current,
                    cookies=cookies or None,
                    headers=extra_headers,
                    impersonate=target,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except curl_requests.RequestsError as e:
                return NetworkFailure(current, _short(e))
            location = response.headers.get("location")
            if response.status_code in REDIRECT_CODES and location:
                response.close()
                next_url = validate_url(urljoin(current, location))
                if isinstance(next_url, InvalidUrl):
                    return next_url
                current = next_url
                redirects.append(current)
                continue
            try:
                body = _read_limited(response, max_bytes)
            except curl_requests.RequestsError as e:
                return NetworkFailure(current, _short(e))
            finally:
                response.close()
        if isinstance(body, _Oversized):
            return TooLarge(current, max_bytes)
        return HttpResponse(
            url=current,
            status=response.status_code,
            reason=response.reason or "",
            headers={k.lower(): v for k, v in response.headers.items() if v is not None},
            content=body,
            redirects=tuple(redirects),
            cookies_sent=cookies_sent,
        )
    return TooManyRedirects(url, MAX_REDIRECTS)


def resolve_pin(url: str, addresses: tuple[str, ...]) -> str | None:
    """A curl RESOLVE entry (`host:port:addr,…`) that makes curl connect to `addresses` instead of
    looking the host up again. None for IP-literal hosts, which curl doesn't look up."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        joined = ",".join(f"[{a}]" if ":" in a else a for a in addresses)
        return f"{host}:{port}:{joined}"
    return None


def _read_limited(response: Response, max_bytes: int) -> bytes | _Oversized:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        return _Oversized()
    chunks: list[bytes] = []
    size = 0
    # curl_cffi leaves iter_content's chunk_size parameter unannotated; chunks are bytes.
    stream = cast("Iterator[bytes]", response.iter_content())  # pyright: ignore[reportUnknownMemberType]
    for chunk in stream:
        size += len(chunk)
        if size > max_bytes:
            return _Oversized()
        chunks.append(chunk)
    return b"".join(chunks)


def _short(error: Exception) -> str:
    text = str(error).strip()
    return (text.splitlines()[0] if text else type(error).__name__)[:300]
