"""Which URLs a session may fetch without asking: ones the user typed or a search returned.

State is one JSON file per Claude Code session, updated under an exclusive lock because hooks for
parallel tool calls can run at the same time.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from agent_web.config import default_state_dir
from agent_web.jsonvalue import as_dict, get_str
from agent_web.urls import normalize

Source = Literal["user", "search"]
MAX_AGE_SECONDS = 7 * 24 * 3600
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def state_dir() -> Path:
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    base = Path(plugin_data) if plugin_data else default_state_dir()
    return base / "provenance"


class Store:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or state_dir()

    def _path(self, session_id: str) -> Path:
        name = (
            session_id
            if _SAFE_ID.match(session_id)
            else hashlib.sha256(session_id.encode()).hexdigest()[:32]
        )
        return self.directory / f"{name}.json"

    def _read(self, raw: str) -> dict[str, str]:
        try:
            data = as_dict(json.loads(raw)) if raw.strip() else None
        except ValueError:
            data = None
        urls = as_dict(data.get("urls")) if data else None
        if urls is None:
            return {}
        return {key: value for key, value in urls.items() if isinstance(value, str)}

    def add(self, session_id: str, urls: Iterable[str], source: Source) -> None:
        normalized = [normalize(url) for url in urls]
        if not normalized:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            known = self._read(handle.read())
            for url in normalized:
                # "user" outranks "search": keep the stronger origin if both apply.
                if known.get(url) != "user":
                    known[url] = source
            handle.seek(0)
            handle.truncate()
            json.dump({"urls": known}, handle)
        self._prune()

    def source_of(self, session_id: str, url: str) -> Source | None:
        path = self._path(session_id)
        try:
            with path.open(encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                known = self._read(handle.read())
        except OSError:
            return None
        match known.get(normalize(url)):
            case "user":
                return "user"
            case "search":
                return "search"
            case _:
                return None

    def _prune(self) -> None:
        cutoff = time.time() - MAX_AGE_SECONDS
        for path in self.directory.glob("*.json"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()


def payload_session(payload: dict[str, object]) -> str:
    return get_str(payload, "session_id") or "unknown-session"
