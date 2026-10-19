"""Reading a real Firefox-format cookie database through browser_cookie3."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

from agent_web.cookies import AllowlistCookieSource, load_browser_cookies
from agent_web.failures import CookieReadFailure
from agent_web.search.google import signed_in


def firefox_db(path: Path, rows: list[tuple[str, str, int, int, str, str]]) -> Path:
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "create table moz_cookies (id integer primary key, host text, path text, "
            "isSecure integer, expiry integer, name text, value text, isHttpOnly integer)"
        )
        db.executemany(
            "insert into moz_cookies (host, path, isSecure, expiry, name, value, isHttpOnly) "
            "values (?, ?, ?, ?, ?, ?, 0)",
            rows,
        )
    return path


def test_reads_firefox_cookies(tmp_path: Path) -> None:
    future = int(time.time()) + 86_400
    db = firefox_db(
        tmp_path / "cookies.sqlite",
        [
            (".reddit.com", "/", 1, future, "reddit_session", "abc"),
            ("www.notreddit.com", "/", 0, future, "other", "x"),
            (".google.com", "/", 1, future, "SID", "sid"),
        ],
    )
    cookies = load_browser_cookies("firefox", "reddit.com", db)
    assert isinstance(cookies, list)
    assert {c.name for c in cookies} >= {"reddit_session"}

    source = AllowlistCookieSource("firefox", ["reddit.com"], cookie_file=db)
    assert source.for_url("https://www.reddit.com/r/x") == {"reddit_session": "abc"}
    assert source.for_url("https://www.notreddit.com/") == {}

    google = load_browser_cookies("firefox", "google.com", db)
    assert isinstance(google, list)
    assert signed_in(google)


def test_missing_or_wrong_file_is_a_failure(tmp_path: Path) -> None:
    missing = load_browser_cookies("firefox", "reddit.com", tmp_path / "nope.sqlite")
    assert isinstance(missing, CookieReadFailure)
    not_cookies = tmp_path / "other.sqlite"
    with contextlib.closing(sqlite3.connect(not_cookies)) as db, db:
        db.execute("create table t (x)")
    assert isinstance(load_browser_cookies("firefox", "reddit.com", not_cookies), CookieReadFailure)


def test_unknown_browser() -> None:
    assert load_browser_cookies("netscape", "x.com", None) == CookieReadFailure(
        "netscape", "unsupported browser"
    )
