"""URL validation, normalization and domain matching."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from agent_web.failures import InvalidUrl, PrivateAddress

MAX_URL_LENGTH = 8192


def validate_url(url: str) -> str | InvalidUrl:
    """The URL (stripped) if it is an absolute http(s) URL without credentials."""
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        return InvalidUrl(url[:80] + "…", f"longer than {MAX_URL_LENGTH} characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return InvalidUrl(url, "only http:// and https:// URLs can be fetched")
    if not parts.hostname:
        return InvalidUrl(url, "no host")
    if parts.username is not None or parts.password is not None:
        return InvalidUrl(url, "embedded credentials are not allowed")
    try:
        _ = parts.port
    except ValueError:
        return InvalidUrl(url, "invalid port")
    return url


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_private_address(host: str) -> bool:
    """True if the host is (or resolves to) a loopback, private, link-local or reserved address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # unresolvable: the HTTP layer reports the DNS error
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return True
    return False


def check_public(url: str, allow_private: bool) -> PrivateAddress | None:
    if allow_private:
        return None
    host = hostname(url)
    if host == "localhost" or host.endswith(".localhost") or is_private_address(host):
        return PrivateAddress(host)
    return None


def domain_matches(host: str, domain: str) -> bool:
    """RFC 6265 style: host equals the domain or is a subdomain of it."""
    host = host.lower().rstrip(".")
    domain = domain.lower().lstrip(".").rstrip(".")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def normalize(url: str) -> str:
    """Canonical form used to recognize the same URL twice (e.g. repeated search results).

    Lower-cases scheme and host, treats http and https as the same, drops default ports,
    fragments and a trailing slash (except the root path). The query string is kept as-is.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, parts.query, ""))
