"""The headless-browser refresh for Google's JavaScript check, with a fake `firefox`."""

from __future__ import annotations

import contextlib
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest

from agent_web.config import Config
from agent_web.search.limiter import Paused, RateLimiter
from agent_web.search.refresh import (
    KEEP_SECONDS,
    BrowserRefresh,
    Refreshed,
    RefreshedCookies,
    profile_dir,
    recent_refreshes,
    refresher_for,
    run_browser,
)
from test_cookies import cookie

# The cookie table of Firefox 158.
SCHEMA = """CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL
DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER,
creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0,
sameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0, isPartitionedAttributeSet INTEGER
DEFAULT 0, updateTime INTEGER,
CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes))"""

# Acts like `firefox --headless --screenshot`: Google's check refreshes __Secure-STRP and sets
# SG_SS. It records which sites' cookies the profile held.
FAKE_FIREFOX = """#!{python}
import sqlite3, sys, time
args = sys.argv[1:]
profile = args[args.index("--profile") + 1]
con = sqlite3.connect(profile + "/cookies.sqlite")
hosts = sorted({{h for (h,) in con.execute("SELECT host FROM moz_cookies")}})
open("{record}", "w").write(" ".join(hosts))
now = int(time.time() * 1000)
con.execute("UPDATE moz_cookies SET value = 'fresh', expiry = ? WHERE name = '__Secure-STRP'",
            (now + 300_000,))
con.execute("INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure) "
            "VALUES ('SG_SS', 'token', 'www.google.com', '/', ?, 1)", (now + 30_000,))
con.commit()
sys.exit({exit_code})
"""


def make_profile(path: Path) -> Path:
    path.mkdir(parents=True)
    now = int(time.time() * 1000)
    with contextlib.closing(sqlite3.connect(path / "cookies.sqlite")) as db, db:
        db.execute(SCHEMA)
        db.executemany(
            "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure) "
            "VALUES (?, ?, ?, '/', ?, 1)",
            [
                ("SID", "sid", ".google.com", now + 10**10),
                ("__Secure-STRP", "stale", ".google.com", now - 60_000),
                ("session", "bank", ".bank.example", now + 10**10),
            ],
        )
    return path


@pytest.fixture
def fake_firefox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `firefox` first on PATH; returns the file it records the copied hosts in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "hosts.txt"
    script = bin_dir / "firefox"
    script.write_text(FAKE_FIREFOX.format(python=sys.executable, record=record, exit_code=0))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return record


def test_run_browser_returns_the_cookies_googles_check_set(
    tmp_path: Path, fake_firefox: Path
) -> None:
    source = make_profile(tmp_path / "profile")
    work = tmp_path / "work"
    got = run_browser("https://www.google.com/search?q=x", "firefox", source, work)
    assert isinstance(got, list)
    assert {(c.domain, c.name, c.value) for c in got} == {
        (".google.com", "__Secure-STRP", "fresh"),
        ("www.google.com", "SG_SS", "token"),
    }
    assert all(c.expires and c.expires < 10**11 for c in got)  # seconds, not milliseconds
    assert fake_firefox.read_text() == ".google.com"  # only Google's cookies were copied
    assert list(work.iterdir()) == []  # the throwaway profile is gone
    assert (source / "cookies.sqlite").stat().st_size > 0  # the user's profile is untouched


def test_run_browser_failures(
    tmp_path: Path, fake_firefox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_profile(tmp_path / "profile")
    url = "https://www.google.com/search?q=x"
    assert run_browser(url, "librewolf", source, tmp_path / "w") == "librewolf isn't installed"
    script = fake_firefox.parent / "bin" / "firefox"
    script.write_text(script.read_text().replace("sys.exit(0)", "sys.exit(3)"))
    assert run_browser(url, "firefox", source, tmp_path / "w") == "firefox exited with 3"
    script.write_text("#!/bin/sh\nexit 0\n")
    assert run_browser(url, "firefox", source, tmp_path / "w") == (
        "the browser got no new Google cookies"
    )
    assert "can't copy" in str(run_browser(url, "firefox", tmp_path / "nope", tmp_path / "w"))


def test_profile_dir(tmp_path: Path) -> None:
    base = Path.home() / ".mozilla" / "firefox"
    base.mkdir(parents=True)
    ini = base / "profiles.ini"
    ini.write_text("[Profile0]\nPath=old.default\nDefault=1\n")
    assert profile_dir("firefox", None) == base / "old.default"
    ini.write_text(ini.read_text() + "[Install4F96D1932A9F858E]\nDefault=new.default-release\n")
    assert profile_dir("firefox", None) == base / "new.default-release"
    assert profile_dir("firefox", tmp_path / "x" / "cookies.sqlite") == tmp_path / "x"
    assert profile_dir("librewolf", None) is None
    assert profile_dir("chrome", None) is None


def test_refresher_for(tmp_path: Path) -> None:
    profile = Config(cookie_file=tmp_path / "p" / "cookies.sqlite", cache_dir=tmp_path / "c")
    assert isinstance(refresher_for(profile), BrowserRefresh)
    assert refresher_for(Config(browser="chrome", cache_dir=tmp_path)) is None
    off = Config(cookie_file=tmp_path / "cookies.sqlite", browser_refresh=False)
    assert refresher_for(off) is None


def test_refreshed_cookies_are_kept_briefly_and_privately(tmp_path: Path) -> None:
    clock = [1000.0]
    store = RefreshedCookies(tmp_path / "google-cookies.json", now=lambda: clock[0])
    store.save(
        [
            cookie("SG_SS", "token", "www.google.com", secure=True, expires=1030),
            cookie("SIDCC", "cc", ".google.com", expires=10**9),
        ]
    )
    assert stat.S_IMODE((tmp_path / "google-cookies.json").stat().st_mode) == 0o600
    assert {(c.name, c.value) for c in store.load()} == {("SG_SS", "token"), ("SIDCC", "cc")}
    clock[0] = 1031  # SG_SS expired
    assert [c.name for c in store.load()] == ["SIDCC"]
    store.save([cookie("SIDCC", "newer", ".google.com", expires=10**9)])
    assert [(c.name, c.value) for c in store.load()] == [("SIDCC", "newer")]
    clock[0] += KEEP_SECONDS + 1  # long-lived cookies are only kept for a while
    assert store.load() == []


def test_browser_refresh_runs_once_a_minute_and_shares_the_result(
    tmp_path: Path, fake_firefox: Path
) -> None:
    clock = [time.time()]
    limiter = RateLimiter(tmp_path / "state", 0, now=lambda: clock[0], sleep=lambda s: None)
    refresh = BrowserRefresh(
        "firefox", make_profile(tmp_path / "p"), tmp_path / "cache", now=lambda: clock[0]
    )
    url = "https://www.google.com/search?q=x"
    first = refresh.refresh(url, limiter)
    assert isinstance(first, Refreshed)
    assert first.how.startswith("headless firefox, ")
    assert {c.name for c in first.cookies} == {"__Secure-STRP", "SG_SS"}
    assert {c.name for c in refresh.cookies()} == {"__Secure-STRP", "SG_SS"}
    fake_firefox.unlink()
    clock[0] += 20  # another process, 20 s later: reuses instead of running the browser
    second = refresh.refresh(url, limiter)
    assert isinstance(second, Refreshed)
    assert second.how == "reused a refresh 20 s old"
    assert not fake_firefox.exists()
    clock[0] += 60
    limiter.pause("its JavaScript check", 600)
    assert isinstance(refresh.refresh(url, limiter), Paused)


def test_refresh_log(tmp_path: Path) -> None:
    config = Config(cache_dir=tmp_path)
    refresh = BrowserRefresh("firefox", tmp_path, tmp_path, now=lambda: 1792611305.0)
    for n in range(5):
        refresh.log(f"event {n}")
    assert recent_refreshes(config) == [
        "2026-10-21 19:35:05 UTC  event 2",
        "2026-10-21 19:35:05 UTC  event 3",
        "2026-10-21 19:35:05 UTC  event 4",
    ]
    assert recent_refreshes(Config(cache_dir=tmp_path / "none")) == []
