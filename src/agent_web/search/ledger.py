"""Per search API: searches this month, and when to try it again after it couldn't answer.

Shared by every process through one JSON file under an exclusive lock (flock), like the Google
limiter. An API that is out of credit is skipped until the next UTC day instead of being asked
(and failing) on every search; one that rejected its key or is limiting requests, for a while.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path

from agent_web.config import Config
from agent_web.jsonvalue import as_dict, as_list, get_float, get_int, get_str
from agent_web.statefile import locked


@dataclass
class _Entry:
    month: str = ""  # "2026-10": what `count` counts
    count: int = 0
    skip_until: float = 0.0
    why: str = ""  # why it's skipped: "is out of credit"
    recent: list[float] = field(default_factory=lambda: list[float]())  # request times (rate limit)


@dataclass(frozen=True)
class Skipped:
    until: float
    why: str


@dataclass(frozen=True)
class AtLimit:
    """The API's rate-limit window is full for longer than a search waits."""

    seconds_left: float  # until its next slot


def clock(timestamp: float) -> str:
    """When a skip ends, for messages: "22:00 UTC"."""
    return time.strftime("%H:%M UTC", time.gmtime(timestamp))


class Ledger:
    def __init__(self, path: Path, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now

    @contextlib.contextmanager
    def _entries(self, write: bool = True) -> Generator[dict[str, _Entry]]:
        """The entries under an exclusive lock, written back if `write`."""
        with locked(self.path) as file:
            try:
                data = as_dict(json.loads(file.text or "{}")) or {}
            except ValueError:
                data = {}
            entries: dict[str, _Entry] = {}
            for name, raw in data.items():
                item = as_dict(raw) or {}
                entries[name] = _Entry(
                    month=get_str(item, "month"),
                    count=get_int(item, "count"),
                    skip_until=get_float(item, "skip_until") or 0.0,
                    why=get_str(item, "why"),
                    recent=[
                        float(t)
                        for t in as_list(item.get("recent")) or []
                        if isinstance(t, int | float)
                    ],
                )
            yield entries
            if write:
                file.new = json.dumps({name: entry.__dict__ for name, entry in entries.items()})

    def _month(self) -> str:
        return time.strftime("%Y-%m", time.gmtime(self._now()))

    def _skipped(self, entry: _Entry | None) -> Skipped | None:
        if entry is None or entry.skip_until <= self._now():
            return None
        return Skipped(entry.skip_until, entry.why)

    def skipped(self, name: str) -> Skipped | None:
        with self._entries(write=False) as entries:
            return self._skipped(entries.get(name))

    def skips(self) -> dict[str, Skipped]:
        """Every API being skipped now, in one read."""
        with self._entries(write=False) as entries:
            found = {name: self._skipped(entry) for name, entry in entries.items()}
            return {name: skipped for name, skipped in found.items() if skipped is not None}

    def skip(self, name: str, until: float, why: str) -> None:
        with self._entries() as entries:
            entry = entries.setdefault(name, _Entry())
            entry.skip_until, entry.why = until, why

    def claim(
        self, name: str, limit: int, window: float, max_wait: float, *, honor_skip: bool = True
    ) -> float | AtLimit | Skipped:
        """A request slot under `name`'s rate limit (`limit` requests per `window` seconds): the
        seconds to wait before sending, with the slot reserved; AtLimit when the next slot is
        more than `max_wait` away; or, if `honor_skip`, Skipped while the API is being skipped.
        One locked read and write, however it turns out."""
        with self._entries() as entries:
            entry = entries.setdefault(name, _Entry())
            if honor_skip and (skipped := self._skipped(entry)) is not None:
                return skipped
            now = self._now()
            entry.recent = [t for t in entry.recent if t > now - window]
            if len(entry.recent) < limit:
                entry.recent.append(now)
                return 0.0
            start = entry.recent[-limit] + window
            if start - now > max_wait:
                return AtLimit(start - now)
            entry.recent.append(start)
            return start - now

    def count(self, name: str) -> None:
        """One more search answered by `name` this month."""
        with self._entries() as entries:
            entry = entries.setdefault(name, _Entry())
            if entry.month != self._month():
                entry.month, entry.count = self._month(), 0
            entry.count += 1
            entry.skip_until, entry.why = 0.0, ""

    def used(self, name: str) -> int:
        """Searches `name` answered this month (as counted since agent-web started counting)."""
        with self._entries(write=False) as entries:
            entry = entries.get(name)
            return entry.count if entry is not None and entry.month == self._month() else 0


def ledger_for(config: Config) -> Ledger:
    return Ledger(config.cache_dir / "search-apis.json")
