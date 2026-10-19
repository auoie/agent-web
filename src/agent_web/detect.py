"""Detecting pages that aren't what was asked for: errors, bot checks, JS-only shells."""

from __future__ import annotations

import re

from agent_web.model import (
    BotCheck,
    HiddenText,
    HttpErrorStatus,
    Issue,
    LikelyNeedsJavaScript,
    NearlyEmpty,
    OutsideMain,
    PdfLayoutWarning,
    ScannedPdf,
)

# Phrases that only appear on interstitial block pages: trusted in titles and short pages.
_BOT_PHRASES = re.compile(
    r"prove your humanity|verify (?:that )?you are (?:a )?human|are you a robot|"
    r"unusual traffic from your computer|just a moment\.\.\.|attention required|"
    r"checking your browser|enable javascript and cookies to continue|access denied",
    re.IGNORECASE,
)
# A weaker signal (articles talk about CAPTCHAs too): only trusted on short pages.
_CAPTCHA_WORD = re.compile(r"\bcaptcha\b", re.IGNORECASE)
SHORT_PAGE_CHARS = 3_000
JS_SHELL_HTML_CHARS = 50_000
JS_SHELL_TEXT_CHARS = 1_500
SCANNED_PDF_CHARS_PER_PAGE = 50
NEARLY_EMPTY_CHARS = 20
OUTSIDE_MAIN_CHARS = 200


def bot_check(title: str, text: str) -> BotCheck | None:
    if match := _BOT_PHRASES.search(title):
        return BotCheck(match.group(0))
    if len(text) < SHORT_PAGE_CHARS:
        short_page = f"{title}\n{text}"
        if match := _BOT_PHRASES.search(short_page) or _CAPTCHA_WORD.search(short_page):
            return BotCheck(match.group(0))
    return None


def html_issues(
    *,
    status: int,
    title: str,
    markdown: str,
    text_chars: int,
    html_chars: int,
    hidden_count: int,
    outside_main_chars: int = 0,
) -> list[Issue]:
    issues: list[Issue] = []
    if status >= 400:
        issues.append(HttpErrorStatus(status))
    if (blocked := bot_check(title, markdown)) is not None:
        issues.append(blocked)
    elif html_chars > JS_SHELL_HTML_CHARS and text_chars < JS_SHELL_TEXT_CHARS:
        issues.append(LikelyNeedsJavaScript(html_chars=html_chars, text_chars=text_chars))
    elif len(markdown.strip()) < NEARLY_EMPTY_CHARS:
        issues.append(NearlyEmpty(len(markdown.strip())))
    if outside_main_chars >= OUTSIDE_MAIN_CHARS:
        issues.append(OutsideMain(outside_main_chars))
    if hidden_count:
        issues.append(HiddenText(hidden_count))
    return issues


def pdf_issues(*, status: int, pages: int, text_chars: int) -> list[Issue]:
    issues: list[Issue] = []
    if status >= 400:
        issues.append(HttpErrorStatus(status))
    if pages and text_chars < SCANNED_PDF_CHARS_PER_PAGE * pages:
        issues.append(ScannedPdf())
    issues.append(PdfLayoutWarning())
    return issues
