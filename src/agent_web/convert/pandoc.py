"""Locating and running pandoc (>= 3.0, for the Figure element used by clean.lua)."""

from __future__ import annotations

import functools
import importlib.util
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_web.failures import ToolMissing

MIN_VERSION = (3, 0)
LUA_FILTER = Path(__file__).with_name("clean.lua")
INSTALL_HINT = (
    "install pandoc >= 3.0 (e.g. `pacman -S pandoc-cli`, `brew install pandoc`), "
    "or install agent-web with the `bundled-pandoc` extra"
)


@dataclass(frozen=True)
class Pandoc:
    path: str
    version: tuple[int, ...]


@dataclass(frozen=True)
class PandocFailed:
    """pandoc ran but failed on this particular input."""

    reason: str


def _bundled_pandoc() -> str | None:
    """The binary shipped by the optional `pypandoc-binary` package, if installed."""
    spec = importlib.util.find_spec("pypandoc")
    if spec is None or spec.origin is None:
        return None
    candidate = Path(spec.origin).parent / "files" / "pandoc"
    return str(candidate) if candidate.is_file() else None


def _version(path: str) -> tuple[int, ...] | str:
    """The version tuple, or a reason string if this pandoc can't be used."""
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=20, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return f"{path} can't be run ({e})"
    match = re.match(r"pandoc(?:\.exe)?\s+(\d+(?:\.\d+)*)", out)
    if not match:
        return f"{path} printed an unexpected --version"
    return tuple(int(part) for part in match.group(1).split("."))


@functools.cache
def find_pandoc(configured: str | None = None) -> Pandoc | ToolMissing:
    """Configured path, else `pandoc` on PATH, else the bundled-pandoc extra."""
    candidates: list[str] = []
    if configured:
        candidates.append(shutil.which(configured) or configured)
    else:
        if found := shutil.which("pandoc"):
            candidates.append(found)
        if bundled := _bundled_pandoc():
            candidates.append(bundled)
    if not candidates:
        return ToolMissing("pandoc", INSTALL_HINT)
    problems: list[str] = []
    for path in candidates:
        version = _version(path)
        if isinstance(version, str):
            problems.append(version)
        elif version < MIN_VERSION:
            problems.append(f"{path} is version {'.'.join(map(str, version))}, need >= 3.0")
        else:
            return Pandoc(path=path, version=version)
    return ToolMissing("pandoc", "; ".join(problems) + f". {INSTALL_HINT}")


def html_to_gfm(pandoc: Pandoc, html: str, timeout: float = 120) -> str | PandocFailed:
    """Convert HTML to GitHub-flavored markdown with the agent-web Lua filter."""
    args = [
        pandoc.path,
        "--from=html",
        "--to=gfm-tex_math_dollars",
        "--wrap=none",
        "--sandbox",  # no file or network access while reading untrusted HTML
        f"--lua-filter={LUA_FILTER}",
    ]
    try:
        result = subprocess.run(
            args, input=html, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return PandocFailed(f"pandoc took longer than {timeout:.0f}s")
    except OSError as e:
        return PandocFailed(f"can't run pandoc: {e}")
    if result.returncode != 0:
        return PandocFailed(
            result.stderr.strip()[:500] or f"pandoc exited with {result.returncode}"
        )
    return result.stdout
