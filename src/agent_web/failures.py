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


@dataclass(frozen=True)
class SearchPaused:
    """Searches are paused after the service blocked one (see search/google.py)."""

    service: str
    cause: str  # what it answered with: "a captcha", "its JavaScript check"
    minutes_left: int


@dataclass(frozen=True)
class SearchBudgetUsed:
    """The agents sent as many searches as search.google_searches_per_10_minutes allows."""

    service: str
    searches: int
    seconds_left: int


@dataclass(frozen=True)
class NoApiKey:
    name: str  # the search API, as in the config: "tavily"
    env: str  # the environment variable for its key


@dataclass(frozen=True)
class SearchApiError:
    """A search API answered with an error status."""

    service: str
    status: int
    detail: str
    retry_after: float = 0.0  # seconds it asked to wait (Retry-After), if any
    used_up: bool = False  # its rate-limit headers say a longer quota (per month) is used up


@dataclass(frozen=True)
class NoEngineAnswered:
    """The first engine couldn't answer (`primary`; None when it was a search API), and neither
    could the search APIs tried after it: (name, why) for each, as in "Brave", "is out of
    credit"."""

    primary: Failure | None
    tried: tuple[tuple[str, str], ...]


# After a captcha or JavaScript check, every process stops searching for this long: retrying
# during a block keeps it going.
SEARCH_PAUSE_MINUTES = 10


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
    | SearchPaused
    | SearchBudgetUsed
    | NoApiKey
    | SearchApiError
    | NoEngineAnswered
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
        case (
            NotSignedIn()
            | Captcha()
            | ConsentRequired()
            | JavaScriptWall()
            | SearchPaused()
            | SearchBudgetUsed()
        ):
            return EXIT_BLOCKED
        case NoApiKey():
            return EXIT_USAGE
        case SearchApiError(status=status):
            if status in (401, 403, 422):
                return EXIT_USAGE
            return EXIT_BLOCKED if status in (402, 429) else EXIT_NETWORK
        case NoEngineAnswered(primary=primary):
            return EXIT_BLOCKED if primary is None else exit_code(primary)
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
                f"{service} answered with a captcha: it is limiting searches, usually after many "
                f"in a short time. Searches are paused for {SEARCH_PAUSE_MINUTES} minutes; "
                "retrying sooner keeps the block going. If it continues after that, open "
                f"{service} in your browser and solve the captcha"
            )
        case ConsentRequired(service):
            return f"{service} shows a consent page: open it in your browser, accept, then retry"
        case JavaScriptWall(service):
            return (
                f"{service} served its JavaScript check instead of results, and agent-web couldn't "
                "get past it: it is limiting searches, usually after many in a short time. "
                f"Searches are paused for {SEARCH_PAUSE_MINUTES} minutes; retrying sooner keeps "
                f"the block going. If it continues after that, the sign-in may have expired: open "
                f"{service} in your browser, search once, then retry"
            )
        case SearchPaused(service, cause, minutes_left):
            return (
                f"{service} searches are paused for {minutes_left} more minute(s) because it "
                f"answered a search with {cause}; retrying sooner keeps the block going. Work with "
                "the pages already found (web fetch still works), or wait"
            )
        case SearchBudgetUsed(service, searches, seconds_left):
            return (
                f"the agents on this machine sent {searches} {service} searches in the last 10 "
                "minutes, the most agent-web allows: more makes "
                f"{service} block searches for everyone. The next one is possible in "
                f"{_duration(seconds_left)}. Work with the pages already found (web fetch still "
                "works), or wait. (With a search API key in agent-web's config, another engine "
                "would answer instead.)"
            )
        case NoApiKey(name, env):
            return (
                f'no API key for {name}: set the {env} environment variable, or {name} = "..." '
                "under [keys] in agent-web's config (web config --example)"
            )
        case SearchApiError(service, status, detail):
            message = f"the {service} API answered HTTP {status}"
            message += f": {detail}" if detail else ""
            if status in (401, 403, 422):
                message += f". Check the {service} API key in agent-web's config"
            elif status in (402, 429):
                message += " (too many requests at once, or the month's credit is used up)"
            return message
        case NoEngineAnswered(primary, tried):
            reasons = "; ".join(f"{name} {why}" for name, why in tried)
            if primary is None:
                return f"no search engine could answer: {reasons}"
            return f"{describe(primary)}. No search API could answer instead: {reasons}"
        case _:
            assert_never(failure)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} s"
    return f"{-(-seconds // 60)} min"
