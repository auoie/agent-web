from __future__ import annotations

from agent_web.detect import bot_check, html_issues
from agent_web.model import (
    BotCheck,
    HiddenText,
    HttpErrorStatus,
    Issue,
    LikelyNeedsJavaScript,
    NearlyEmpty,
    OutsideMain,
)


def issues(
    *,
    status: int = 200,
    title: str = "An article",
    markdown: str = "A long article. " * 500,
    text_chars: int = 8000,
    html_chars: int = 60_000,
    hidden_count: int = 0,
    outside_main_chars: int = 0,
) -> list[Issue]:
    return html_issues(
        status=status,
        title=title,
        markdown=markdown,
        text_chars=text_chars,
        html_chars=html_chars,
        hidden_count=hidden_count,
        outside_main_chars=outside_main_chars,
    )


def test_normal_page_has_no_issues() -> None:
    assert issues() == []


def test_http_error() -> None:
    assert HttpErrorStatus(404) in issues(status=404)


def test_bot_check_by_title() -> None:
    assert bot_check("Reddit - Prove your humanity", "x" * 10_000) == BotCheck(
        "Prove your humanity"
    )


def test_bot_check_by_short_page_text() -> None:
    assert isinstance(bot_check("", "Just a moment... checking your browser"), BotCheck)


def test_articles_about_captchas_are_not_bot_checks() -> None:
    long_text = "x" * 5000 + " a captcha is a test " + "y" * 5000
    assert bot_check("What is a CAPTCHA?", long_text) is None
    assert bot_check("Article", long_text) is None


def test_short_page_with_captcha_is_a_bot_check() -> None:
    assert bot_check("Security check", "Please complete the CAPTCHA below") == BotCheck("CAPTCHA")


def test_javascript_shell() -> None:
    found = issues(markdown="Loading...", text_chars=10, html_chars=300_000)
    assert LikelyNeedsJavaScript(html_chars=300_000, text_chars=10) in found


def test_bot_check_takes_precedence_over_js_shell() -> None:
    found = issues(title="Just a moment...", markdown="", text_chars=0, html_chars=300_000)
    assert any(isinstance(i, BotCheck) for i in found)
    assert not any(isinstance(i, LikelyNeedsJavaScript) for i in found)


def test_nearly_empty_page_is_flagged() -> None:
    assert issues(markdown="", text_chars=0, html_chars=600) == [NearlyEmpty(0)]
    assert issues(markdown="  Hi ", text_chars=2, html_chars=600) == [NearlyEmpty(2)]
    assert issues(markdown="A short but real page.", html_chars=600) == []


def test_text_outside_main_is_reported() -> None:
    assert issues(outside_main_chars=199) == []
    assert issues(outside_main_chars=200) == [OutsideMain(200)]


def test_hidden_text_count() -> None:
    assert HiddenText(3) in issues(hidden_count=3)
