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
from agent_web.invisible import HIDDEN_LABEL

MAX_HTML_CHARS = 5_000_000
_DROP_TAGS = (
    "script",
    "style",
    "template",
    "svg",
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

# Elements that belong in <head>. libxml2's parser predates HTML5: when a page omits the optional
# <body> tag, it puts HTML5 elements it doesn't know (main, section, article, header, nav, figure,
# time, ...) and everything after them into <head>, where pandoc ignores them.
_HEAD_TAGS = frozenset({"title", "base", "link", "meta", "style", "script", "template"})
# When a page has exactly one <main>, pandoc converts only that element. Text outside it in these
# elements is the boilerplate that behavior is meant to drop; other outside text is reported.
_BOILERPLATE_TAGS = frozenset({"nav", "header", "footer", "aside", "menu", "form", "button"})

# Text of "permalink to this heading" anchors (Sphinx, MkDocs, GitHub, ...): noise for a reader.
_PERMALINK_TEXT = frozenset({"¶", "#", "§", "🔗", "link", "permalink", "anchor"})
# What some sites put in an otherwise empty anchor (Mintlify writes a zero-width space).
_ZERO_WIDTH = "​‌‍⁠﻿"

# Inside a heading, pandoc ends the heading at these: layout blocks wrapped around the title
# (Mintlify uses <div>s) and line breaks. They are unwrapped.
_HEADING_SPLITTERS = ("div", "p", "section", "article", "header", "footer", "br")

# Hidden fragments shorter than this (counters, icons) can't carry an instruction; they stay in
# the text unlabeled instead of triggering a note.
MIN_HIDDEN_CHARS = 12

NOSCRIPT_LABEL = "shown only without JavaScript"


@dataclass(frozen=True)
class Prepared:
    html: str
    title: str
    hidden_count: int
    text_chars: int
    outside_main_chars: int = 0
    # Visible text pandoc should keep: the single <main> if the page has one, else everything.
    expected_chars: int = 0


@dataclass(frozen=True)
class HtmlConversion:
    markdown: str
    title: str
    hidden_count: int
    text_chars: int
    html_chars: int
    outside_main_chars: int = 0
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


def _rescue_head_content(doc: lxml.html.HtmlElement) -> None:
    """Move body content that the parser misplaced into <head> back to the start of <body>."""
    head = doc.find("head")
    if head is None:
        return
    children = list(head)
    first = next(
        (
            i
            for i, child in enumerate(children)
            if isinstance(child.tag, str) and child.tag not in _HEAD_TAGS
        ),
        None,
    )
    if first is None:
        return
    body = doc.find("body")
    if body is None:
        body = etree.SubElement(doc, "body")
    for index, element in enumerate(children[first:]):
        body.insert(index, element)  # moves the element (and its tail text) out of <head>


def _tidy_same_page_links(doc: lxml.html.HtmlElement, page_url: str) -> None:
    """Drop permalink anchors and shorten other links within the page to `#fragment`.

    Repeating the full page URL on every in-page cross-reference costs a lot of tokens on
    documentation pages without telling the reader anything.
    """
    page = page_url.split("#", 1)[0]
    for link in list(doc.iter("a")):
        if not isinstance(link, lxml.html.HtmlElement):
            continue
        href = link.get("href") or ""
        if not href.startswith(page + "#"):
            continue
        text = " ".join(link.text_content().split()).strip(_ZERO_WIDTH)
        # An icon (svg) is dropped later anyway; an image is content.
        if text.lower() in _PERMALINK_TEXT | {""} and next(link.iter("img"), None) is None:
            _remove(link)
        else:
            link.set("href", href[len(page) :])


def _inline_heading_wrappers(doc: lxml.html.HtmlElement) -> None:
    """Keep each heading on one line through pandoc.

    pandoc turns `<h2><div>Title</div></h2>` into an empty `##` and a separate paragraph, drops
    the heading around a `<section>` or `<header>`, and breaks it at a `<br>`. (A `<p>` directly
    inside a heading can't be helped: libxml2 already ended the heading there while parsing.)
    """
    for heading in doc.iter("h1", "h2", "h3", "h4", "h5", "h6"):
        for wrapper in list(heading.iterdescendants(*_HEADING_SPLITTERS)):
            if not isinstance(wrapper, lxml.html.HtmlElement):
                continue
            # Hidden text has already been labeled. Keep inline children and a space at each
            # former block boundary so unwrapping doesn't join neighboring words.
            wrapper.text = " " + (wrapper.text or "")
            wrapper.tail = " " + (wrapper.tail or "")
            wrapper.drop_tag()


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


def _complete_dates(doc: lxml.html.HtmlElement) -> None:
    """Add the machine-readable timestamp to dates shown without a year.

    `<time datetime="2025-10-10T15:34Z">10 Oct 15:34</time>` (GitHub writes dates like this and
    lets a script finish them in the browser) becomes `10 Oct 15:34 (2025-10-10T15:34Z)`.
    """
    for element in doc.iter("time", "relative-time"):
        if not isinstance(element, lxml.html.HtmlElement):
            continue
        stamp = (element.get("datetime") or "").strip()
        if len(stamp) < 4 or not stamp[:4].isdigit() or stamp[:4] in element.text_content():
            continue
        children = list(element)
        if children:
            children[-1].tail = (children[-1].tail or "") + f" ({stamp})"
        else:
            element.text = (element.text or "") + f" ({stamp})"


def _chars_outside(element: lxml.html.HtmlElement, skip: frozenset[lxml.html.HtmlElement]) -> int:
    """Visible text length under `element`, leaving out the `skip` subtrees and boilerplate tags."""
    total = len("".join((element.text or "").split()))
    for child in element:
        if (
            isinstance(child, lxml.html.HtmlElement)
            and child not in skip
            and child.tag not in _BOILERPLATE_TAGS
        ):
            total += _chars_outside(child, skip)
        total += len("".join((child.tail or "").split()))
    return total


def _visible_text_chars(element: lxml.html.HtmlElement) -> int:
    return len(" ".join(element.text_content().split()))


def prepare(html_text: str, base_url: str, whole_page: bool = False) -> Prepared:
    """Clean the HTML for pandoc and collect facts used by problem detection.

    `whole_page` turns <main> into a plain <div> so pandoc converts the entire page.
    """
    doc = parse_document(_XML_DECL.sub("", html_text, count=1))
    if doc is None:
        return Prepared(html="", title="", hidden_count=0, text_chars=0)

    title = " ".join((doc.findtext(".//title") or "").split())
    _rescue_head_content(doc)
    doc.make_links_absolute(base_url, resolve_base_href=True, handle_failures="discard")
    _tidy_same_page_links(doc, base_url)
    _complete_dates(doc)

    for node in list(doc.iter(*_DROP_TAGS, etree.Comment)):
        _remove(node)
    # pandoc turns MathML into TeX math. Hidden MathML is a copy for screen readers next to a
    # visible image of the formula (Wikipedia), so it goes instead of being labeled hidden.
    for math in list(doc.iter("math")):
        if any(
            _is_hidden(e)
            for e in (math, *math.iterancestors())
            if isinstance(e, lxml.html.HtmlElement)
        ):
            _remove(math)

    labeled: set[lxml.html.HtmlElement] = set()
    for element in doc.iter():
        if not isinstance(element, lxml.html.HtmlElement):
            continue
        is_noscript = element.tag == "noscript"
        if not (is_noscript or _is_hidden(element)):
            continue
        if any(ancestor in labeled for ancestor in element.iterancestors()):
            continue
        if len("".join(element.text_content().split())) < MIN_HIDDEN_CHARS:
            continue
        _wrap_text(element, NOSCRIPT_LABEL if is_noscript else HIDDEN_LABEL)
        for attribute in ("style", "hidden"):
            if attribute in element.attrib:
                del element.attrib[attribute]
        labeled.add(element)

    mains = [m for m in doc.iter("main") if isinstance(m, lxml.html.HtmlElement)]
    text_chars = _visible_text_chars(doc)
    outside_main = 0
    expected = text_chars
    hidden_count = len(labeled)
    if whole_page:
        for main in mains:
            main.tag = "div"
    elif len(mains) == 1:
        expected = _visible_text_chars(mains[0])
        body = doc.find("body")
        if isinstance(body, lxml.html.HtmlElement):
            outside_main = _chars_outside(body, frozenset(mains))
        # Only labels inside <main> reach the output.
        hidden_count = sum(1 for e in labeled if e is mains[0] or mains[0] in e.iterancestors())

    _inline_heading_wrappers(doc)
    return Prepared(
        html=lxml.html.tostring(doc, encoding="unicode"),
        title=title,
        hidden_count=hidden_count,
        text_chars=text_chars,
        outside_main_chars=outside_main,
        expected_chars=expected,
    )


def _tidy(markdown: str) -> str:
    lines = [line.rstrip() for line in markdown.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _plain_text(html: str) -> str:
    doc = parse_document(html)
    if doc is None:
        return ""
    return re.sub(r"[ \t]*\n\s*\n\s*", "\n\n", doc.text_content()).strip()


def convert_html(
    data: bytes, content_type: str, base_url: str, pandoc: Pandoc, whole_page: bool = False
) -> HtmlConversion:
    text = decode_html(data, content_type)
    prepared = prepare(text, base_url, whole_page)
    html = prepared.html
    fallback: str | None = None
    if len(html) > MAX_HTML_CHARS:
        html = html[:MAX_HTML_CHARS]
        fallback = f"page HTML was cut to its first {MAX_HTML_CHARS:,} characters before converting"
    converted = html_to_gfm(pandoc, html) if html else ""
    if isinstance(converted, PandocFailed):
        markdown = _plain_text(html)
        fallback = f"pandoc failed ({converted.reason}); showing plain text without structure"
    elif prepared.expected_chars >= 200 and len(converted.strip()) < prepared.expected_chars // 2:
        markdown = _plain_text(html)
        fallback = (
            f"the converted page kept only {len(converted.strip()):,} of "
            f"{prepared.expected_chars:,} characters of visible text; showing plain text without "
            "structure instead"
        )
    else:
        markdown = converted
    return HtmlConversion(
        markdown=_tidy(markdown),
        title=prepared.title,
        hidden_count=prepared.hidden_count,
        text_chars=prepared.text_chars,
        html_chars=len(text),
        outside_main_chars=prepared.outside_main_chars,
        fallback_reason=fallback,
    )
