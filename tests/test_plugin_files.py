"""The Claude Code plugin files are consistent with each other and with the package."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from agent_web import __version__
from agent_web.jsonvalue import as_dict, as_list, get_str

ROOT = Path(__file__).resolve().parents[1]
HOOK_EVENTS = {"pre-tool-use", "user-prompt-submit", "post-tool-use"}


def load_json(path: Path) -> dict[str, object]:
    data = as_dict(json.loads(path.read_text(encoding="utf-8")))
    assert data is not None, path
    return data


def test_versions_match() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = load_json(ROOT / ".claude-plugin" / "plugin.json")
    assert pyproject["project"]["version"] == __version__ == get_str(plugin, "version")


def test_marketplace_lists_this_plugin() -> None:
    plugin = load_json(ROOT / ".claude-plugin" / "plugin.json")
    marketplace = load_json(ROOT / ".claude-plugin" / "marketplace.json")
    entries = [as_dict(e) or {} for e in as_list(marketplace.get("plugins")) or []]
    assert [get_str(e, "name") for e in entries] == [get_str(plugin, "name")]
    assert get_str(entries[0], "source") == "./"


def test_hooks_point_at_executable_scripts_with_known_events() -> None:
    hooks = as_dict(load_json(ROOT / "hooks" / "hooks.json").get("hooks")) or {}
    assert set(hooks) == {"PreToolUse", "UserPromptSubmit", "PostToolUse"}
    seen_events: set[str] = set()
    for groups in hooks.values():
        for group in as_list(groups) or []:
            for handler in as_list((as_dict(group) or {}).get("hooks")) or []:
                entry = as_dict(handler) or {}
                command = get_str(entry, "command").replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
                assert os.access(command, os.X_OK), command
                args = [str(a) for a in as_list(entry.get("args")) or []]
                assert len(args) == 1
                seen_events.add(args[0])
    assert seen_events == HOOK_EVENTS


def test_bash_hooks_are_narrowed_with_if() -> None:
    hooks = as_dict(load_json(ROOT / "hooks" / "hooks.json").get("hooks")) or {}
    for event in ("PreToolUse", "PostToolUse"):
        for group in as_list(hooks[event]) or []:
            group_dict = as_dict(group) or {}
            if get_str(group_dict, "matcher") != "Bash":
                continue
            for handler in as_list(group_dict.get("hooks")) or []:
                assert get_str(as_dict(handler) or {}, "if").startswith("Bash(web ")


def test_launcher_is_executable() -> None:
    assert os.access(ROOT / "bin" / "web", os.X_OK)


def test_skill_frontmatter() -> None:
    text = (ROOT / "skills" / "web" / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md needs YAML frontmatter"
    fields = dict(line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line)
    assert fields["name"] == "web"
    assert "WebFetch" in fields["description"]
    for command in ("web search", "web fetch", "--find", "--offset"):
        assert command in text
