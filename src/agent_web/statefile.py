"""Files in agent-web's cache folder: state shared by every process (read and rewritten under an
exclusive lock), and files replaced atomically so a reader never sees half of one."""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Locked:
    text: str  # the file's contents when the lock was taken
    new: str | None = None  # set to replace them when the lock is released


@contextlib.contextmanager
def locked(path: Path) -> Generator[Locked]:
    """`path`'s contents under an exclusive lock (flock) shared by every process. The file is
    rewritten only if `new` was set, so reading doesn't write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        state = Locked(handle.read())
        yield state
        if state.new is not None:
            handle.seek(0)
            handle.truncate()
            handle.write(state.new)


def atomic_write(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step. The file is readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")  # created as 0600
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def unwritable(directory: Path) -> str | None:
    """Why files can't be written in `directory`, or None if they can (checked by writing one: a
    sandbox can refuse what the permission bits allow)."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory, prefix=".probe-"):
            pass
    except OSError as e:
        return e.strerror or type(e).__name__
    return None
