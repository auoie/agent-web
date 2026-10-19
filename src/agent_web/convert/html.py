"""HTML to markdown: an lxml clean-up pass, then pandoc with clean.lua.

The lxml pass removes non-content elements, makes links absolute and labels text that human
visitors can't see (inline-hidden elements, <noscript>), so injected instructions hidden from
people are visible as such to the agent instead of being silently mixed into the page.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

import lxml.html
from lxml import etree

from agent_web.convert.pandoc import Pandoc, PandocFailed, html_to_gfm

MAX_HTML_CHARS = 5_000_000
_DROP_TAGS = (
    "script",
    "style",
    "template",
    "svg",
    "math",
    "iframe",
    "object",
    "embed",
    "canvas",
    "noembed",
    "link",
    "meta",
    "base",
)
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:"
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*0*(?:\.0+)?\s*(?:!important\s*)?(?:;|$)"
    r"|font-size\s*:\s*0+(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:!important\s*)?(?:;|$)"
    r"|font-size\s*:\s*(?:[0-2](?:\.\d+)?px|0?\.[0-2]\d*r?em)\s*(?:!important\s*)?(?:;|$)"
    r")",
    re.IGNORECASE,
)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", re.IGNORECASE)

HIDDEN_LABEL = "hidden on page"
NOSCRIPT_LABEL = "shown only without JavaScript"


@dataclass(frozen=True)
class Prepared:
    html: str
    title: str
    hidden_count: int
    text_chars: int


@dataclass(frozen=True)
class HtmlConversion:
    markdown: str
    title: str
    hidden_count: int
    text_chars: int
    html_chars: int
    fallback_reason: str | None = None


def _codec(name: str) -> str | None:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _declared_codec(data: bytes, content_type: str) -> str | None:
    header = _HEADER_CHARSET.search(content_type)
    if header and (codec := _codec(header.group(1))):
        return codec
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return codec
    meta = _META_CHARSET.search(data[:4096])
    if meta and (codec := _codec(meta.group(1).decode("ascii", "replace"))):
        return codec
    return None


def decode_html(data: bytes, content_type: str) -> str:
    """Decode with the header charset, a BOM, a <meta> charset, UTF-8, then Windows-1252."""
    if codec := _declared_codec(data, content_type):
        return data.decode(codec, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_document(html_text: str) -> lxml.html.HtmlElement | None:
    """Parse into an lxml.html tree, or None for empty or unparseable input."""
    if not html_text.strip():
        return None
    try:
        root = lxml.html.document_fromstring(html_text)  # pyright: ignore[reportUnknownMemberType]
    except (etree.ParserError, ValueError):
        return None
    return root if isinstance(root, lxml.html.HtmlElement) else None


def _remove(node: etree._Element) -> None:  # pyright: ignore[reportPrivateUsage]
    """Remove a node but keep its tail text (what follows it in the parent)."""
    parent = node.getparent()
    if parent is None:
        return
    if node.tail:
        previous = node.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
    parent.remove(node)


def _is_hidden(element: lxml.html.HtmlElement) -> bool:
    if element.get("hidden") is not None:
        return True
    style = element.get("style")
    return bool(style and _HIDDEN_STYLE.search(style))


def _wrap_text(element: lxml.html.HtmlElement, label: str) -> None:
    """Put `⟦label: … ⟧` around the element's text, inside the element.

    These brackets survive pandoc unescaped, unlike `[` and `]`.
    """
    element.text = f"⟦{label}: " + (element.text or "")
    children = list(element)
    if children:
        children[-1].tail = (children[-1].tail or "") + "⟧"
    else:
        element.text += "⟧"


def _visible_text_chars(element: lxml.html.HtmlElement) -> int:
    return len(" ".join(element.text_content().split()))


def prepare(html_text: str, base_url: str) -> Prepared:
    """Clean the HTML for pandoc and collect facts used by problem detection."""
    doc = parse_document(_XML_DECL.sub("", html_text, count=1))
    if doc is None:
        return Prepared(html="", title="", hidden_count=0, text_chars=0)

    title = " ".join((doc.findtext(".//title") or "").split())
    doc.make_links_absolute(base_url, resolve_base_href=True, handle_failures="discard")

    for node in list(doc.iter(*_DROP_TAGS, etree.Comment)):
        _remove(node)

    labeled: set[lxml.html.HtmlElement] = set()
    for element in doc.iter():
        if not isinstance(element, lxml.html.HtmlElement):
            continue
        is_noscript = element.tag == "noscript"
        if not (is_noscript or _is_hidden(element)):
            continue
        if any(ancestor in labeled for ancestor in element.iterancestors()):
            continue
        if not element.text_content().strip():
            continue
        _wrap_text(element, NOSCRIPT_LABEL if is_noscript else HIDDEN_LABEL)
        for attribute in ("style", "hidden"):
            if attribute in element.attrib:
                del element.attrib[attribute]
        labeled.add(element)

    return Prepared(
        html=lxml.html.tostring(doc, encoding="unicode"),
        title=title,
        hidden_count=len(labeled),
        text_chars=_visible_text_chars(doc),
    )


def _tidy(markdown: str) -> str:
    lines = [line.rstrip() for line in markdown.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _plain_text(html: str) -> str:
    doc = parse_document(html)
    if doc is None:
        return ""
    return re.sub(r"[ \t]*\n\s*\n\s*", "\n\n", doc.text_content()).strip()


def convert_html(data: bytes, content_type: str, base_url: str, pandoc: Pandoc) -> HtmlConversion:
    text = decode_html(data, content_type)
    prepared = prepare(text, base_url)
    html = prepared.html
    fallback: str | None = None
    if len(html) > MAX_HTML_CHARS:
        html = html[:MAX_HTML_CHARS]
        fallback = f"page HTML was cut to its first {MAX_HTML_CHARS:,} characters before converting"
    converted = html_to_gfm(pandoc, html) if html else ""
    if isinstance(converted, PandocFailed):
        markdown = _plain_text(html)
        fallback = f"pandoc failed ({converted.reason}); showing plain text without structure"
    else:
        markdown = converted
    return HtmlConversion(
        markdown=_tidy(markdown),
        title=prepared.title,
        hidden_count=prepared.hidden_count,
        text_chars=prepared.text_chars,
        html_chars=len(text),
        fallback_reason=fallback,
    )
