"""What a fetch produces, as sum types: one variant per kind of content, one per kind of issue."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Meta:
    """Facts about the HTTP exchange, shared by every content variant."""

    url: str
    final_url: str
    status: int
    content_type: str
    cookies_sent: bool
    fetched_at: float


@dataclass(frozen=True)
class HtmlPage:
    meta: Meta
    title: str
    markdown: str


@dataclass(frozen=True)
class PdfDocument:
    meta: Meta
    markdown: str
    pages: int
    saved_to: str


@dataclass(frozen=True)
class RedditThread:
    meta: Meta
    title: str
    markdown: str
    comments: int
    not_loaded: int


@dataclass(frozen=True)
class PlainText:
    meta: Meta
    text: str


@dataclass(frozen=True)
class BinaryFile:
    meta: Meta
    saved_to: str
    size: int


Content = HtmlPage | PdfDocument | RedditThread | PlainText | BinaryFile


@dataclass(frozen=True)
class HttpErrorStatus:
    status: int


@dataclass(frozen=True)
class BotCheck:
    evidence: str


@dataclass(frozen=True)
class LikelyNeedsJavaScript:
    html_chars: int
    text_chars: int


@dataclass(frozen=True)
class HiddenText:
    count: int


@dataclass(frozen=True)
class ScannedPdf:
    pass


@dataclass(frozen=True)
class PdfLayoutWarning:
    pass


@dataclass(frozen=True)
class CookiesMightHelp:
    domain: str


@dataclass(frozen=True)
class ConversionFallback:
    reason: str


Issue = (
    HttpErrorStatus
    | BotCheck
    | LikelyNeedsJavaScript
    | HiddenText
    | ScannedPdf
    | PdfLayoutWarning
    | CookiesMightHelp
    | ConversionFallback
)


@dataclass(frozen=True)
class FetchResult:
    content: Content
    issues: tuple[Issue, ...]
    from_cache: bool = False
