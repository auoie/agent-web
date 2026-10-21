"""Spacing and pausing Google requests, shared by every process (a file with flock).

Every agent on the machine searches with the user's one Google account.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

from agent_web.config import Config
from agent_web.jsonvalue import as_dict, get_float, get_str


@dataclass
class _LimiterState:
    last: float = 0.0  # when the last request started
    paused_until: float = 0.0
    cause: str = ""
    last_refresh: float = 0.0  # when a browser last ran Google's JavaScript check (refresh.py)


def _parse_state(text: str) -> _LimiterState:
    try:
        data = as_dict(json.loads(text))
    except ValueError:
        data = None
    if data is None:
        try:
            return _LimiterState(last=float(text.strip()))  # the older format: just `last`
        except ValueError:
            return _LimiterState()
    return _LimiterState(
        last=get_float(data, "last") or 0.0,
        paused_until=get_float(data, "paused_until") or 0.0,
        cause=get_str(data, "cause"),
        last_refresh=get_float(data, "last_refresh") or 0.0,
    )


@dataclass(frozen=True)
class Paused:
    seconds_left: float
    cause: str


class RateLimiter:
    """Keeps at least `min_interval` seconds between Google requests, and pauses searches after
    Google blocks one, across processes: every agent on the machine uses one Google account."""

    def __init__(
        self,
        path: Path,
        min_interval: float,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = path
        self.min_interval = min_interval
        self._now = now
        self._sleep = sleep

    @contextlib.contextmanager
    def _state(self) -> Generator[_LimiterState]:
        """The shared state, read and written back under an exclusive lock (flock)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            state = _parse_state(handle.read())
            yield state
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(state.__dict__))

    def wait(self) -> Paused | None:
        """Wait until `min_interval` has passed since the last request and claim the next slot.
        While searches are paused, return the pause instead, without waiting."""
        with self._state() as state:
            now = self._now()
            if state.paused_until > now:
                return Paused(state.paused_until - now, state.cause)
            delay = state.last + self.min_interval - now
            if delay > 0:
                self._sleep(min(delay, self.min_interval))
            state.last = self._now()
        return None

    def pause(self, cause: str, seconds: float) -> None:
        with self._state() as state:
            state.paused_until = self._now() + seconds
            state.cause = cause

    def since_refresh(self) -> float:
        """Seconds since the last browser refresh (see refresh.py)."""
        with self._state() as state:
            return self._now() - state.last_refresh

    def note_refresh(self) -> None:
        with self._state() as state:
            state.last_refresh = self._now()

    def paused(self) -> Paused | None:
        with self._state() as state:
            left = state.paused_until - self._now()
            return Paused(left, state.cause) if left > 0 else None


def limiter_for(config: Config) -> RateLimiter:
    return RateLimiter(config.cache_dir / "google-last-request", config.search_min_interval)
