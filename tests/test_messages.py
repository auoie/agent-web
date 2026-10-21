"""Every failure and issue variant has an exit code and a human-readable message."""

from __future__ import annotations

import pytest

from agent_web.failures import (
    EXIT_BLOCKED,
    EXIT_NETWORK,
    EXIT_USAGE,
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    FallbackFailed,
    InvalidConfig,
    InvalidUrl,
    JavaScriptWall,
    NetworkFailure,
    NoApiKey,
    NotSignedIn,
    PrivateAddress,
    SearchApiError,
    SearchBudgetUsed,
    SearchPaused,
    TooLarge,
    ToolMissing,
    TooManyRedirects,
    describe,
    exit_code,
)
from agent_web.model import (
    BotCheck,
    ConversionFallback,
    CookiesMightHelp,
    HiddenText,
    HttpErrorStatus,
    Issue,
    LikelyNeedsJavaScript,
    NearlyEmpty,
    OutsideMain,
    PdfLayoutWarning,
    ScannedPdf,
)
from agent_web.output import describe_issue

FAILURES: list[tuple[Failure, int, str]] = [
    (InvalidConfig("bad key"), EXIT_USAGE, "bad key"),
    (ToolMissing("pandoc", "install it"), EXIT_USAGE, "install it"),
    (CookieReadFailure("firefox", "locked"), EXIT_USAGE, "locked"),
    (InvalidUrl("ftp://x", "only http"), EXIT_USAGE, "ftp://x"),
    (PrivateAddress("10.0.0.1"), EXIT_USAGE, "allow_private_networks"),
    (NetworkFailure("https://x.io", "timeout"), EXIT_NETWORK, "timeout"),
    (TooLarge("https://x.io", 25 * 1024 * 1024), EXIT_NETWORK, "25 MB"),
    (TooManyRedirects("https://x.io", 10), EXIT_NETWORK, "10 redirects"),
    (NotSignedIn("Google", "firefox"), EXIT_BLOCKED, "sign in"),
    (Captcha("Google"), EXIT_BLOCKED, "captcha"),
    (ConsentRequired("Google"), EXIT_BLOCKED, "consent"),
    (JavaScriptWall("Google"), EXIT_BLOCKED, "paused for 10 minutes"),
    (Captcha("Google"), EXIT_BLOCKED, "retrying sooner keeps the block going"),
    (SearchPaused("Google", "a captcha", 7), EXIT_BLOCKED, "paused for 7 more minute(s)"),
    (SearchBudgetUsed("Google", 20, 95), EXIT_BLOCKED, "next one is possible in 2 min"),
    (SearchBudgetUsed("Google", 20, 40), EXIT_BLOCKED, "next one is possible in 40 s"),
    (NoApiKey("Brave"), EXIT_USAGE, "BRAVE_SEARCH_API_KEY"),
    (SearchApiError("Brave", 401, ""), EXIT_USAGE, "Check the Brave Search API key"),
    (SearchApiError("Brave", 429, "slow down"), EXIT_BLOCKED, "HTTP 429: slow down"),
    (SearchApiError("Brave", 500, ""), EXIT_NETWORK, "answered HTTP 500"),
    (
        FallbackFailed(Captcha("Google"), NoApiKey("Brave")),
        EXIT_BLOCKED,
        "Brave couldn't answer instead: no Brave Search API key",
    ),
]


@pytest.mark.parametrize(("failure", "code", "fragment"), FAILURES)
def test_failure_messages_and_codes(failure: Failure, code: int, fragment: str) -> None:
    assert exit_code(failure) == code
    assert fragment in describe(failure)


ISSUES: list[tuple[Issue, str]] = [
    (HttpErrorStatus(404), "HTTP 404"),
    (BotCheck("captcha"), "bot-check"),
    (LikelyNeedsJavaScript(100_000, 10), "JavaScript"),
    (NearlyEmpty(0), "almost no text"),
    (OutsideMain(500), "--whole-page"),
    (HiddenText(2), "⟦hidden on page"),
    (ScannedPdf(), "scanned"),
    (PdfLayoutWarning(), "original PDF"),
    (CookiesMightHelp("reddit.com"), "fetch.cookie_domains"),
    (ConversionFallback("pandoc failed"), "pandoc failed"),
]


@pytest.mark.parametrize(("issue", "fragment"), ISSUES)
def test_issue_messages(issue: Issue, fragment: str) -> None:
    assert fragment in describe_issue(issue)
