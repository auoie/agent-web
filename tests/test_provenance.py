from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_web.provenance import Store, state_dir


def test_add_and_lookup(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.add("s1", ["https://example.com/a/", "http://Example.com/b#x"], "user")
    assert store.source_of("s1", "https://example.com/a") == "user"
    assert store.source_of("s1", "https://example.com/b") == "user"
    assert store.source_of("s1", "https://example.com/c") is None
    assert store.source_of("other-session", "https://example.com/a") is None


def test_user_outranks_search(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.add("s", ["https://a.example/"], "search")
    assert store.source_of("s", "https://a.example/") == "search"
    store.add("s", ["https://a.example/"], "user")
    store.add("s", ["https://a.example/"], "search")
    assert store.source_of("s", "https://a.example/") == "user"


def test_unsafe_session_ids_are_hashed(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.add("../../etc/passwd", ["https://a.example/"], "user")
    assert store.source_of("../../etc/passwd", "https://a.example/") == "user"
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


def test_corrupt_state_is_ignored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    (tmp_path / "s.json").write_text("{broken")
    assert store.source_of("s", "https://a.example/") is None
    store.add("s", ["https://a.example/"], "user")
    assert store.source_of("s", "https://a.example/") == "user"


def test_concurrent_adds_lose_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)

    def add(i: int) -> None:
        store.add("s", [f"https://site{i}.example/"], "search")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(store.source_of("s", f"https://site{i}.example/") == "search" for i in range(40))


def test_state_dir_uses_plugin_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    assert state_dir() == tmp_path / "plugin" / "provenance"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    assert state_dir() == tmp_path / "state" / "agent-web" / "provenance"
