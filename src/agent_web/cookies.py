"""Reading cookies from the user's browser and choosing which ones a request may carry."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from agent_web.failures import CookieReadFailure
from agent_web.urls import domain_matches

# curl_cffi impersonation target matching the browser whose cookies we send, so the TLS
# fingerprint and cookies tell the same story.
_IMPERSONATE = {"firefox": "firefox", "librewolf": "firefox"}
DEFAULT_IMPERSONATE = "chrome"
# An expiry above this is in milliseconds (in seconds, it would be past the year 5000).
MILLISECONDS_THRESHOLD = 100_000_000_000

CookieLoader = Callable[[str, str, Path | None], "list[Cookie] | CookieReadFailure"]


def impersonation_for(browser: str) -> str:
    return _IMPERSONATE.get(browser, DEFAULT_IMPERSONATE)


class CookieSource(Protocol):
    """The cookies a request to `url` should carry: a dict (possibly empty) or a failure."""

    def for_url(self, url: str) -> dict[str, str] | CookieReadFailure: ...


def _browser_loader(browser: str) -> Callable[..., CookieJar] | None:
    import browser_cookie3

    loaders: dict[str, Callable[..., CookieJar]] = {
        "firefox": browser_cookie3.firefox,
        "librewolf": browser_cookie3.librewolf,
        "chrome": browser_cookie3.chrome,
        "chromium": browser_cookie3.chromium,
        "brave": browser_cookie3.brave,
        "vivaldi": browser_cookie3.vivaldi,
        "edge": browser_cookie3.edge,
        "opera": browser_cookie3.opera,
    }
    return loaders.get(browser)


def load_browser_cookies(
    browser: str, domain: str, cookie_file: Path | None
) -> list[Cookie] | CookieReadFailure:
    """The browser's cookies whose domain contains `domain` (callers filter further)."""
    import browser_cookie3

    loader = _browser_loader(browser)
    if loader is None:
        return CookieReadFailure(browser, "unsupported browser")
    try:
        jar = loader(cookie_file=str(cookie_file) if cookie_file else None, domain_name=domain)
    except browser_cookie3.BrowserCookieError as e:
        return CookieReadFailure(browser, str(e))
    except Exception as e:
        # browser_cookie3 raises assorted errors besides its own (sqlite, keyring, decryption,
        # and its own bugs: an AttributeError for a non-cookie database, see
        # tests/test_browser_cookies.py). Reading cookies failing must never crash `web`.
        return CookieReadFailure(browser, f"{type(e).__name__}: {e}")
    cookies = list(jar)
    for cookie in cookies:
        # Recent Firefox stores expiry in milliseconds and browser_cookie3 passes it through, so
        # no cookie looked expired and stale ones were sent (Google's short-lived __Secure-STRP).
        if cookie.expires and cookie.expires > MILLISECONDS_THRESHOLD:
            cookie.expires //= 1000
    return cookies


def _path_matches(request_path: str, cookie_path: str) -> bool:
    if not cookie_path or cookie_path == "/":
        return True
    if request_path == cookie_path:
        return True
    prefix = cookie_path if cookie_path.endswith("/") else cookie_path + "/"
    return request_path.startswith(prefix)


def cookies_for_url(
    cookies: Iterable[Cookie], url: str, now: float | None = None
) -> dict[str, str]:
    """Cookies a browser would send to `url`: domain, path, secure flag and expiry are honored.

    When several cookies share a name, the most specific (longest path, then longest domain) wins.
    """
    now = time.time() if now is None else now
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    secure = parts.scheme.lower() == "https"
    chosen: dict[str, tuple[int, int, str]] = {}
    for cookie in cookies:
        if cookie.value is None:
            continue
        domain = cookie.domain.lstrip(".").lower()
        if not domain_matches(host, domain):
            continue
        if cookie.secure and not secure:
            continue
        if cookie.expires and cookie.expires < now:
            continue
        if not _path_matches(path, cookie.path):
            continue
        rank = (len(cookie.path or ""), len(domain))
        current = chosen.get(cookie.name)
        if current is None or rank > current[:2]:
            chosen[cookie.name] = (rank[0], rank[1], cookie.value)
    return {name: value for name, (_, _, value) in chosen.items()}


class AllowlistCookieSource:
    """Browser cookies, but only for URLs on allowlisted domains (subdomains included)."""

    def __init__(
        self,
        browser: str,
        domains: Iterable[str],
        cookie_file: Path | None = None,
        loader: CookieLoader = load_browser_cookies,
    ) -> None:
        self.browser = browser
        self.domains = tuple(domains)
        self.cookie_file = cookie_file
        self._loader = loader
        self._cache: dict[str, list[Cookie]] = {}

    def allowed_domain(self, url: str) -> str | None:
        host = (urlsplit(url).hostname or "").lower()
        return next((d for d in self.domains if domain_matches(host, d)), None)

    def for_url(self, url: str) -> dict[str, str] | CookieReadFailure:
        domain = self.allowed_domain(url)
        if domain is None:
            return {}
        if domain not in self._cache:
            loaded = self._loader(self.browser, domain, self.cookie_file)
            if isinstance(loaded, CookieReadFailure):
                return loaded
            self._cache[domain] = loaded
        return cookies_for_url(self._cache[domain], url)
