from __future__ import annotations

import json

import pytest

from agent_web.sites.reddit import json_url, parse_and_render, render_thread, thread_id
from conftest import FIXTURES

THREAD = (FIXTURES / "reddit" / "thread.json").read_bytes()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.reddit.com/r/ClaudeAI/comments/1vim8b7/psa_be_careful/", "1vim8b7"),
        ("https://old.reddit.com/r/x/comments/ABC123", "abc123"),
        ("https://reddit.com/comments/1x9ef9c/", "1x9ef9c"),
        ("https://redd.it/1x9ef9c", "1x9ef9c"),
        ("https://www.reddit.com/r/ClaudeAI/", None),
        ("https://www.reddit.com/user/someone/comments/", None),
        ("https://notreddit.com/r/x/comments/abc123/", None),
        ("https://example.com/comments/abc123/", None),
    ],
)
def test_thread_id(url: str, expected: str | None) -> None:
    assert thread_id(url) == expected


def test_json_url() -> None:
    assert json_url("abc").startswith("https://www.reddit.com/comments/abc.json?raw_json=1")


def test_render_thread() -> None:
    rendered = parse_and_render(THREAD)
    assert rendered is not None
    assert rendered.title == "PSA: Be careful letting Claude use WebFetch"
    assert rendered.comments == 4
    assert rendered.not_loaded == 5
    md = rendered.markdown
    assert md.startswith("# PSA: Be careful letting Claude use WebFetch\n")
    assert "- Subreddit: r/ClaudeAI" in md
    assert "- Score: 977 (98% upvoted)" in md
    assert "- Comments: 6 reported, 4 loaded, 5 more not loaded" in md
    assert "- Flair: Claude Code" in md
    assert "WebFetch uses a smaller model.\n\nCheck the source!" in md
    assert "#### [1] u/AutoModerator [moderator] [stickied] · score hidden ·" in md
    assert "#### [2] u/Comfortable-Hall-188 · 84 pts ·" in md
    assert "> I use Trafilatura.\n>\n> It removes clutter." in md
    assert "#### [2.1] u/dink_182 [OP] · 5 pts" in md
    assert "↳ reply to u/Comfortable-Hall-188" in md
    assert "_[3 more replies not loaded]_" in md
    assert "#### [3] u/[deleted] · 13 pts" in md
    assert "_[2 more replies not loaded]_" in md


@pytest.mark.parametrize(
    "payload",
    [None, {}, [], [{}], [{"data": {"children": []}}, {"data": {"children": []}}], ["x", "y"]],
)
def test_render_rejects_non_threads(payload: object) -> None:
    assert render_thread(payload) is None


def test_parse_rejects_non_json() -> None:
    assert parse_and_render(b"<html>blocked</html>") is None


def test_thread_without_comments() -> None:
    data = json.loads(THREAD)
    data[1]["data"]["children"] = []
    rendered = render_thread(data)
    assert rendered is not None
    assert rendered.comments == 0
    assert "_(no comments)_" in rendered.markdown
