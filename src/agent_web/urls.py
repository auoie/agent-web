"""URL validation, normalization and domain matching."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from agent_web.failures import InvalidUrl, NetworkFailure, PrivateAddress

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


def is_public_ip(address: str) -> bool:
    """False for loopback, private, link-local, carrier-grade NAT (Tailscale), reserved and
    multicast addresses."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def public_addresses(url: str) -> tuple[str, ...] | PrivateAddress | NetworkFailure:
    """The addresses of the URL's host, provided every one of them is public.

    The caller connects to exactly these addresses, so a second DNS answer pointing at a local
    service (DNS rebinding) is never used.
    """
    host = hostname(url)
    if host == "localhost" or host.endswith(".localhost"):
        return PrivateAddress(host)
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:
        return NetworkFailure(url, f"can't resolve {host} ({e})")
    addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses or not all(is_public_ip(a) for a in addresses):
        return PrivateAddress(host)
    return addresses


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
