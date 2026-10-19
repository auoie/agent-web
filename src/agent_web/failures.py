"""Expected failures as a sum type.

Functions that can fail in expected ways return `T | Failure` instead of raising, so failures are
part of their signatures and pyright makes callers deal with them. Exceptions are reserved for
bugs. `exit_code` and `describe` match exhaustively: adding a variant without handling it there is
a type error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, assert_never


@dataclass(frozen=True)
class InvalidConfig:
    message: str


@dataclass(frozen=True)
class ToolMissing:
    tool: str
    hint: str


@dataclass(frozen=True)
class CookieReadFailure:
    browser: str
    reason: str


@dataclass(frozen=True)
class InvalidUrl:
    url: str
    reason: str


@dataclass(frozen=True)
class PrivateAddress:
    host: str


@dataclass(frozen=True)
class NetworkFailure:
    url: str
    reason: str


@dataclass(frozen=True)
class TooLarge:
    url: str
    limit_bytes: int


@dataclass(frozen=True)
class TooManyRedirects:
    url: str
    limit: int


@dataclass(frozen=True)
class NotSignedIn:
    service: str
    browser: str


@dataclass(frozen=True)
class Captcha:
    service: str


@dataclass(frozen=True)
class ConsentRequired:
    service: str


@dataclass(frozen=True)
class JavaScriptWall:
    service: str


Failure: TypeAlias = (
    InvalidConfig
    | ToolMissing
    | CookieReadFailure
    | InvalidUrl
    | PrivateAddress
    | NetworkFailure
    | TooLarge
    | TooManyRedirects
    | NotSignedIn
    | Captcha
    | ConsentRequired
    | JavaScriptWall
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NETWORK = 2
EXIT_HTTP_ERROR = 3
EXIT_BLOCKED = 4


def exit_code(failure: Failure) -> int:
    match failure:
        case (
            InvalidConfig() | ToolMissing() | CookieReadFailure() | InvalidUrl() | PrivateAddress()
        ):
            return EXIT_USAGE
        case NetworkFailure() | TooLarge() | TooManyRedirects():
            return EXIT_NETWORK
        case NotSignedIn() | Captcha() | ConsentRequired() | JavaScriptWall():
            return EXIT_BLOCKED
        case _:
            assert_never(failure)


def describe(failure: Failure) -> str:
    match failure:
        case InvalidConfig(message):
            return f"config error: {message}"
        case ToolMissing(tool, hint):
            return f"{tool} not available: {hint}"
        case CookieReadFailure(browser, reason):
            return f"can't read {browser} cookies: {reason}"
        case InvalidUrl(url, reason):
            return f"invalid URL {url!r}: {reason}"
        case PrivateAddress(host):
            return (
                f"refusing to fetch {host}: it is a local or private network address "
                "(set fetch.allow_private_networks = true to allow)"
            )
        case NetworkFailure(url, reason):
            return f"request to {url} failed: {reason}"
        case TooLarge(url, limit):
            return (
                f"{url} is larger than the {limit // 1024 // 1024} MB limit (fetch.max_download_mb)"
            )
        case TooManyRedirects(url, limit):
            return f"more than {limit} redirects starting at {url}"
        case NotSignedIn(service, browser):
            return (
                f"not signed in to {service} in {browser}: sign in there (or set cookies.browser "
                "in the config) and try again"
            )
        case Captcha(service):
            return (
                f"{service} answered with a captcha: open it in your browser, solve the captcha, "
                "then try again in a few minutes"
            )
        case ConsentRequired(service):
            return f"{service} shows a consent page: open it in your browser, accept, then retry"
        case JavaScriptWall(service):
            return (
                f"{service} served its JavaScript check instead of results; your cookies may have "
                "expired. Open it in your browser, search once, then retry"
            )
        case _:
            assert_never(failure)
