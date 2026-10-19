"""The skill file is well-formed and documents the commands; the version is consistent."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from agent_web import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_versions_match() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_skill_frontmatter() -> None:
    text = (ROOT / "skills" / "web" / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md needs YAML frontmatter"
    fields = dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)
    assert fields["name"] == "web"
    assert "`web`" in fields["description"]
    for command in ("web search", "web fetch", "--find", "--offset", "--whole-page"):
        assert command in text
