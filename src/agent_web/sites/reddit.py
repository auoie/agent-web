"""Reddit threads via the `.json` endpoint, rendered as a post plus a numbered comment tree."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from agent_web.jsonvalue import as_dict, as_list, get_bool, get_float, get_int, get_str
from agent_web.urls import domain_matches

_COMMENTS_PATH = re.compile(r"^/(?:r/[^/]+/)?comments/([a-z0-9]{2,12})(?:/|$)", re.IGNORECASE)
_SHORT_PATH = re.compile(r"^/([a-z0-9]{2,12})/?$", re.IGNORECASE)


def thread_id(url: str) -> str | None:
    """The post id for Reddit thread URLs (reddit.com/.../comments/<id>/..., redd.it/<id>)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if domain_matches(host, "reddit.com"):
        match = _COMMENTS_PATH.match(parts.path)
    elif host == "redd.it":
        match = _SHORT_PATH.match(parts.path)
    else:
        return None
    return match.group(1).lower() if match else None


def json_url(post_id: str) -> str:
    return f"https://www.reddit.com/comments/{post_id}.json?raw_json=1&limit=500&depth=12"


@dataclass(frozen=True)
class RenderedThread:
    title: str
    markdown: str
    comments: int
    not_loaded: int


@dataclass
class _Tally:
    comments: int = 0
    not_loaded: int = 0
    lines: list[str] = field(default_factory=list[str])


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "?"
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _quote(text: str) -> str:
    text = text.strip() or "(empty)"
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def _render_comments(
    children: list[object], prefix: str, parent: str | None, op: str, tally: _Tally
) -> None:
    number = 0
    for child in children:
        thing = as_dict(child)
        data = as_dict(thing.get("data")) if thing else None
        if thing is None or data is None:
            continue
        kind = get_str(thing, "kind")
        if kind == "more":
            missing = get_int(data, "count") or len(as_list(data.get("children")) or [])
            if missing:
                tally.not_loaded += missing
                tally.lines.append(f"_[{missing} more replies not loaded]_\n")
            continue
        if kind != "t1":
            continue
        number += 1
        tally.comments += 1
        path = f"{prefix}{number}"
        author = get_str(data, "author", "[deleted]")
        tags = " [OP]" if author == op and author != "[deleted]" else ""
        if get_str(data, "distinguished"):
            tags += f" [{get_str(data, 'distinguished')}]"
        if get_bool(data, "stickied"):
            tags += " [stickied]"
        score = (
            "score hidden" if get_bool(data, "score_hidden") else f"{get_int(data, 'score')} pts"
        )
        tally.lines.append(
            f"#### [{path}] u/{author}{tags} · {score} · {_when(get_float(data, 'created_utc'))}"
        )
        if parent is not None:
            tally.lines.append(f"↳ reply to u/{parent}")
        tally.lines.append("")
        tally.lines.append(_quote(get_str(data, "body")))
        tally.lines.append("")
        replies = as_dict(data.get("replies"))
        reply_data = as_dict(replies.get("data")) if replies else None
        reply_children = as_list(reply_data.get("children")) if reply_data else None
        if reply_children:
            _render_comments(reply_children, f"{path}.", author, op, tally)


def render_thread(payload: object) -> RenderedThread | None:
    """Render the decoded `.json` payload, or None if it doesn't look like a thread."""
    listings = as_list(payload)
    if listings is None or len(listings) < 2:
        return None
    first = as_dict(listings[0])
    post_listing = as_dict(first.get("data")) if first else None
    if post_listing is None:
        return None
    post_children = as_list(post_listing.get("children")) or []
    post_thing = as_dict(post_children[0]) if post_children else None
    post = as_dict(post_thing.get("data")) if post_thing else None
    comment_listing = as_dict(listings[1])
    comment_data = as_dict(comment_listing.get("data")) if comment_listing else None
    comments = as_list(comment_data.get("children")) if comment_data else None
    if post is None or comments is None:
        return None

    title = get_str(post, "title").strip()
    op = get_str(post, "author", "[deleted]")
    tally = _Tally()
    _render_comments(comments, "", None, op, tally)

    ratio = get_float(post, "upvote_ratio")
    lines = [
        f"# {title}",
        "",
        f"- Subreddit: r/{get_str(post, 'subreddit')}",
        f"- Author: u/{op}",
        f"- Posted: {_when(get_float(post, 'created_utc'))}",
        f"- Score: {get_int(post, 'score')}"
        + (f" ({ratio:.0%} upvoted)" if ratio is not None else ""),
        f"- Comments: {get_int(post, 'num_comments')} reported, {tally.comments} loaded"
        + (f", {tally.not_loaded} more not loaded" if tally.not_loaded else ""),
    ]
    if not get_bool(post, "is_self") and (link := get_str(post, "url")):
        lines.append(f"- Link: {link}")
    if flair := get_str(post, "link_flair_text"):
        lines.append(f"- Flair: {flair}")
    body = get_str(post, "selftext").strip()
    lines += ["", "## Post", "", body or "_(no text)_", "", "## Comments", ""]
    lines += tally.lines or ["_(no comments)_"]
    return RenderedThread(
        title=title,
        markdown="\n".join(lines).strip() + "\n",
        comments=tally.comments,
        not_loaded=tally.not_loaded,
    )


def parse_and_render(data: bytes) -> RenderedThread | None:
    try:
        payload: object = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return render_thread(payload)
