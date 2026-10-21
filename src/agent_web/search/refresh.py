"""Getting past Google's JavaScript check with a real browser.

When Google suspects automation, it answers a search with a page of JavaScript that a browser runs
to prove it is one, and agent-web can't run JavaScript. A headless Firefox can: given a throwaway
copy of the user's Google cookies, it loads the search, passes the check in about a second, and
gets short-lived cookies (`SG_SS` for 30 seconds, `__Secure-STRP` for 5 minutes) that let the next
request through. Those cookies are kept for a few minutes and sent on top of the browser's own.

Only Firefox and LibreWolf are supported: their cookie database can be copied into a fresh profile.
The user's running browser and profile are never touched.
"""

from __future__ import annotations

import configparser
import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from typing import Protocol

from agent_web.config import Config
from agent_web.cookies import MILLISECONDS_THRESHOLD
from agent_web.jsonvalue import as_dict, as_list, get_bool, get_float, get_str
from agent_web.search.limiter import OverBudget, Paused, RateLimiter

# At most one browser refresh a minute, across processes; a search blocked sooner reuses the last
# refresh's cookies or pauses.
REFRESH_GAP_SECONDS = 60
# Refreshed cookies override the browser's for at most this long, so long-lived ones the refresh
# happened to touch (SIDCC) don't shadow newer ones from the user's browsing.
KEEP_SECONDS = 600
BROWSER_TIMEOUT_SECONDS = 60
_SELECT_GOOGLE = (
    "SELECT host, name, path, value, expiry, isSecure FROM moz_cookies "
    "WHERE host = 'google.com' OR host LIKE '%.google.com'"
)
_DELETE_OTHERS = (
    "DELETE FROM moz_cookies WHERE NOT (host = 'google.com' OR host LIKE '%.google.com')"
)
_PREFS = """\
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("app.update.auto", false);
"""
_LOG_LIMIT_BYTES = 100_000


@dataclass(frozen=True)
class Refreshed:
    cookies: list[Cookie]
    how: str  # for the log: "headless firefox, 1.2 s, 5 cookies" or "reused a refresh 12 s old"


class Refresher(Protocol):
    def cookies(self) -> list[Cookie]: ...

    def refresh(self, url: str, limiter: RateLimiter) -> Refreshed | Paused | str: ...

    def log(self, line: str) -> None: ...


def _profile_bases(browser: str) -> list[Path]:
    home = Path.home()
    support = home / "Library" / "Application Support"
    return {
        "firefox": [home / ".mozilla" / "firefox", support / "Firefox"],
        "librewolf": [home / ".librewolf", support / "librewolf"],
    }.get(browser, [])


def _binaries(browser: str) -> list[str]:
    return {
        "firefox": ["firefox", "/Applications/Firefox.app/Contents/MacOS/firefox"],
        "librewolf": ["librewolf", "/Applications/LibreWolf.app/Contents/MacOS/librewolf"],
    }.get(browser, [])


def profile_dir(browser: str, cookie_file: Path | None) -> Path | None:
    """The browser profile whose cookies agent-web reads: the configured cookie file's folder, or
    the default profile from profiles.ini."""
    if cookie_file is not None:
        return cookie_file.parent
    for base in _profile_bases(browser):
        ini = configparser.RawConfigParser()
        if not ini.read(base / "profiles.ini"):
            continue
        for section in ini.sections():
            if section.startswith("Install") and ini[section].get("Default"):
                return base / ini[section]["Default"]
        for section in ini.sections():
            if ini[section].get("Default") == "1" and ini[section].get("Path"):
                return base / ini[section]["Path"]
    return None


def browser_binary(browser: str) -> str | None:
    return next((found for name in _binaries(browser) if (found := shutil.which(name))), None)


@dataclass(frozen=True)
class _Row:
    host: str
    name: str
    path: str
    value: str
    expires: int  # seconds
    secure: bool


def _seconds(expiry: int) -> int:
    return expiry // 1000 if expiry > MILLISECONDS_THRESHOLD else expiry


def _google_rows(db: Path) -> dict[tuple[str, str, str], _Row]:
    with contextlib.closing(sqlite3.connect(db)) as con:
        rows: list[tuple[str, str, str, str | None, int | None, int]] = con.execute(
            _SELECT_GOOGLE
        ).fetchall()
    return {
        (host, name, path): _Row(host, name, path, value or "", _seconds(expiry or 0), bool(secure))
        for host, name, path, value, expiry, secure in rows
    }


def _seed(profile: Path, source: Path) -> None:
    """A fresh profile whose cookie database holds only the user's Google cookies."""
    profile.mkdir(mode=0o700)
    for suffix in ("", "-wal"):
        if (source / f"cookies.sqlite{suffix}").is_file():
            shutil.copyfile(source / f"cookies.sqlite{suffix}", profile / f"cookies.sqlite{suffix}")
    with contextlib.closing(sqlite3.connect(profile / "cookies.sqlite")) as con:
        con.execute(_DELETE_OTHERS)
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    (profile / "cookies.sqlite-wal").unlink(missing_ok=True)
    (profile / "user.js").write_text(_PREFS, encoding="utf-8")


def _cookie(row: _Row) -> Cookie:
    dot = row.host.startswith(".")
    return Cookie(
        version=0,
        name=row.name,
        value=row.value,
        port=None,
        port_specified=False,
        domain=row.host,
        domain_specified=dot,
        domain_initial_dot=dot,
        path=row.path or "/",
        path_specified=True,
        secure=row.secure,
        expires=row.expires,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )


def run_browser(
    url: str, browser: str, source: Path, workdir: Path, timeout: float = BROWSER_TIMEOUT_SECONDS
) -> list[Cookie] | str:
    """The Google cookies a headless browser set or changed while loading `url`, or why it
    couldn't. The throwaway profile, which holds session cookies, is always deleted."""
    binary = browser_binary(browser)
    if binary is None:
        return f"{browser} isn't installed"
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workdir, prefix="refresh-") as tmp:
        profile = Path(tmp) / "profile"
        try:
            _seed(profile, source)
            before = _google_rows(profile / "cookies.sqlite")
        except (OSError, sqlite3.Error) as e:
            return f"can't copy {browser}'s cookies from {source}: {e}"
        command = [
            binary,
            "--headless",
            "--no-remote",
            "--profile",
            str(profile),
            "--window-size",
            "1280,1000",
            "--screenshot",
            str(Path(tmp) / "page.png"),  # loads the page, then exits
            url,
        ]
        try:
            run = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return f"{browser} took longer than {timeout:.0f} s"
        except OSError as e:
            return f"can't run {browser}: {e}"
        if run.returncode != 0:
            return f"{browser} exited with {run.returncode}"
        try:
            after = _google_rows(profile / "cookies.sqlite")
        except sqlite3.Error as e:
            return f"can't read the cookies {browser} got: {e}"
    changed = [row for key, row in after.items() if before.get(key) != row]
    return [_cookie(row) for row in changed] if changed else "the browser got no new Google cookies"


class RefreshedCookies:
    """The Google cookies refreshes got, in a file only the user can read, each kept until it
    expires or for KEEP_SECONDS, whichever is sooner."""

    def __init__(self, path: Path, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now

    def _entries(self) -> list[dict[str, object]]:
        try:
            data = as_list(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []
        entries = [entry for item in data or [] if (entry := as_dict(item)) is not None]
        now = self._now()
        return [e for e in entries if (get_float(e, "keep_until") or 0) > now]

    def load(self) -> list[Cookie]:
        rows = (
            _Row(
                get_str(e, "host"),
                get_str(e, "name"),
                get_str(e, "path", "/"),
                get_str(e, "value"),
                int(get_float(e, "keep_until") or 0),
                get_bool(e, "secure"),
            )
            for e in self._entries()
        )
        return [_cookie(row) for row in rows if row.host and row.name]

    def save(self, cookies: list[Cookie]) -> None:
        now = self._now()
        new = {
            (c.domain, c.name, c.path): {
                "host": c.domain,
                "name": c.name,
                "path": c.path,
                "value": c.value or "",
                "secure": bool(c.secure),
                "keep_until": min(float(c.expires or now), now + KEEP_SECONDS),
            }
            for c in cookies
        }
        kept = [
            e
            for e in self._entries()
            if (get_str(e, "host"), get_str(e, "name"), get_str(e, "path", "/")) not in new
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")  # created as 0600
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump([*kept, *new.values()], handle)
        Path(tmp).replace(self.path)


@contextlib.contextmanager
def _exclusive(path: Path) -> Generator[None]:
    """One refresh at a time across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class BrowserRefresh:
    """A Refresher that runs the user's Firefox or LibreWolf headless."""

    def __init__(
        self, browser: str, source: Path, cache_dir: Path, now: Callable[[], float] = time.time
    ) -> None:
        self.browser = browser
        self.source = source
        self.cache_dir = cache_dir
        self.store = RefreshedCookies(cache_dir / "google-cookies.json", now)
        self.log_path = cache_dir / "google-refresh.log"
        self._now = now

    def cookies(self) -> list[Cookie]:
        return self.store.load()

    def refresh(self, url: str, limiter: RateLimiter) -> Refreshed | Paused | str:
        """Fresh cookies to retry with: from a refresh another process made in the last minute, or
        from running the browser now, which takes a request slot like any search."""
        with _exclusive(self.cache_dir / "google-refresh.lock"):
            if (age := limiter.since_refresh()) < REFRESH_GAP_SECONDS:
                stored = self.store.load()
                if not stored:
                    return f"a refresh {age:.0f} s ago got nothing to reuse"
                return Refreshed(stored, f"reused a refresh {age:.0f} s old")
            waited = limiter.wait()
            if isinstance(waited, OverBudget):
                return f"the Google budget is used up ({waited.requests} requests in 10 minutes)"
            if waited is not None:
                return waited
            limiter.note_refresh()
            started = self._now()
            got = run_browser(url, self.browser, self.source, self.cache_dir / "refresh")
            if isinstance(got, str):
                return got
            self.store.save(got)
            took = self._now() - started
            return Refreshed(self.store.load(), f"headless {self.browser}, {took:.1f} s")

    def log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self._now()))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {line}\n")
        if self.log_path.stat().st_size > _LOG_LIMIT_BYTES:
            lines = self.log_path.read_text(encoding="utf-8").splitlines(keepends=True)
            self.log_path.write_text("".join(lines[len(lines) // 2 :]), encoding="utf-8")


def refresher_for(config: Config) -> BrowserRefresh | None:
    """The browser refresh for this config, or None when it's off or the browser isn't supported."""
    if not config.browser_refresh or not _binaries(config.browser):
        return None
    source = profile_dir(config.browser, config.cookie_file)
    return BrowserRefresh(config.browser, source, config.cache_dir) if source else None


def recent_refreshes(config: Config, count: int = 3) -> list[str]:
    try:
        lines = (config.cache_dir / "google-refresh.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-count:]
