"""Configuration: ~/.config/agent-web/config.toml plus a few environment overrides.

Unknown keys are errors, so typos don't silently fall back to defaults.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from agent_web.failures import InvalidConfig

BROWSERS = ("firefox", "librewolf", "chrome", "chromium", "brave", "vivaldi", "edge", "opera")

EXAMPLE_CONFIG = """\
# agent-web configuration. Every key is optional.

[cookies]
# Browser whose cookies are used: firefox, librewolf, chrome, chromium, brave, vivaldi, edge, opera
browser = "firefox"
# cookie_file = "~/.mozilla/firefox/xxxxxxxx.default-release/cookies.sqlite"

[search]
# Minimum seconds between Google requests (protects your account from rate limits)
min_interval_seconds = 2.0

[fetch]
# Domains that receive your browser cookies when fetched (subdomains included).
# Every other site gets an anonymous request.
cookie_domains = []
# Domains whose URLs agents may fetch without asking, even links found inside pages (Claude Code
# plugin). Only list sites where an attacker can't see the requests (no personal sites, URL
# shorteners, redirect services or paste sites).
trusted_domains = []
timeout_seconds = 30
max_download_mb = 25
# Allow fetching localhost / private network addresses (off: protects local services)
allow_private_networks = false

[output]
# Characters printed per fetch (Claude Code truncates Bash output at 30,000)
max_chars = 25000

[cache]
dir = "~/.cache/agent-web"
ttl_seconds = 3600
max_mb = 200

[convert]
# pandoc executable (>= 3.0). Default: `pandoc` on PATH, then the bundled-pandoc extra.
# pandoc = "/usr/bin/pandoc"
"""


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "agent-web"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "agent-web"


def config_path() -> Path:
    explicit = os.environ.get("AGENT_WEB_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agent-web" / "config.toml"


@dataclass(frozen=True)
class Config:
    browser: str = "firefox"
    cookie_file: Path | None = None
    search_min_interval: float = 2.0
    cookie_domains: tuple[str, ...] = ()
    trusted_domains: tuple[str, ...] = ()
    timeout: float = 30.0
    max_download_bytes: int = 25 * 1024 * 1024
    allow_private_networks: bool = False
    max_chars: int = 25_000
    cache_dir: Path = field(default_factory=_default_cache_dir)
    cache_ttl: float = 3600.0
    cache_max_bytes: int = 200 * 1024 * 1024
    pandoc: str | None = None


_SCHEMA: dict[str, frozenset[str]] = {
    "cookies": frozenset({"browser", "cookie_file"}),
    "search": frozenset({"min_interval_seconds"}),
    "fetch": frozenset(
        {
            "cookie_domains",
            "trusted_domains",
            "timeout_seconds",
            "max_download_mb",
            "allow_private_networks",
        }
    ),
    "output": frozenset({"max_chars"}),
    "cache": frozenset({"dir", "ttl_seconds", "max_mb"}),
    "convert": frozenset({"pandoc"}),
}


class _Invalid(Exception):
    """Internal: aborts parsing. Never leaves this module (see parse_config)."""


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise _Invalid(f"[{name}] must be a table")
    return value  # pyright: ignore[reportUnknownVariableType]


def _number(table: Mapping[str, Any], key: str, where: str, minimum: float) -> float | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Invalid(f"{where}.{key} must be a number")
    if value < minimum:
        raise _Invalid(f"{where}.{key} must be >= {minimum:g}")
    return float(value)


def _string(table: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise _Invalid(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _domain(raw: object, key: str) -> str:
    if not isinstance(raw, str):
        raise _Invalid(f"fetch.{key} must be a list of strings")
    domain = raw.strip().lower().lstrip(".")
    if not domain or any(c in domain for c in "/: \t"):
        raise _Invalid(f"fetch.{key}: {raw!r} is not a bare domain like 'reddit.com'")
    return domain


def _domains(table: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    if key not in table:
        return None
    value: object = table[key]
    if not isinstance(value, list):
        raise _Invalid(f"fetch.{key} must be a list of strings")
    items = cast("list[object]", value)
    return tuple(sorted({_domain(item, key) for item in items}))


def _check_keys(data: Mapping[str, Any]) -> None:
    for name, value in data.items():
        if name not in _SCHEMA:
            raise _Invalid(f"unknown section [{name}]")
        if isinstance(value, dict):
            for key in value:  # pyright: ignore[reportUnknownVariableType]
                if key not in _SCHEMA[name]:
                    raise _Invalid(f"unknown key {name}.{key}")


def _parse(data: Mapping[str, Any]) -> Config:
    _check_keys(data)
    config = Config()

    cookies = _table(data, "cookies")
    if (browser := _string(cookies, "browser", "cookies")) is not None:
        if browser.lower() not in BROWSERS:
            raise _Invalid(f"cookies.browser must be one of: {', '.join(BROWSERS)}")
        config = replace(config, browser=browser.lower())
    if (cookie_file := _string(cookies, "cookie_file", "cookies")) is not None:
        config = replace(config, cookie_file=Path(cookie_file).expanduser())

    search = _table(data, "search")
    if (interval := _number(search, "min_interval_seconds", "search", 0)) is not None:
        config = replace(config, search_min_interval=interval)

    fetch = _table(data, "fetch")
    if (cookie_domains := _domains(fetch, "cookie_domains")) is not None:
        config = replace(config, cookie_domains=cookie_domains)
    if (trusted_domains := _domains(fetch, "trusted_domains")) is not None:
        config = replace(config, trusted_domains=trusted_domains)
    if (timeout := _number(fetch, "timeout_seconds", "fetch", 1)) is not None:
        config = replace(config, timeout=timeout)
    if (max_mb := _number(fetch, "max_download_mb", "fetch", 1)) is not None:
        config = replace(config, max_download_bytes=int(max_mb * 1024 * 1024))
    if "allow_private_networks" in fetch:
        allow: object = fetch["allow_private_networks"]
        if not isinstance(allow, bool):
            raise _Invalid("fetch.allow_private_networks must be true or false")
        config = replace(config, allow_private_networks=allow)

    output = _table(data, "output")
    if (max_chars := _number(output, "max_chars", "output", 500)) is not None:
        config = replace(config, max_chars=int(max_chars))

    cache = _table(data, "cache")
    if (cache_dir := _string(cache, "dir", "cache")) is not None:
        config = replace(config, cache_dir=Path(cache_dir).expanduser())
    if (ttl := _number(cache, "ttl_seconds", "cache", 0)) is not None:
        config = replace(config, cache_ttl=ttl)
    if (cache_mb := _number(cache, "max_mb", "cache", 1)) is not None:
        config = replace(config, cache_max_bytes=int(cache_mb * 1024 * 1024))

    convert = _table(data, "convert")
    if (pandoc := _string(convert, "pandoc", "convert")) is not None:
        config = replace(config, pandoc=pandoc)
    return config


def parse_config(data: Mapping[str, Any]) -> Config | InvalidConfig:
    try:
        return _parse(data)
    except _Invalid as e:
        return InvalidConfig(str(e))


def apply_env(config: Config) -> Config:
    if cache_dir := os.environ.get("AGENT_WEB_CACHE_DIR"):
        config = replace(config, cache_dir=Path(cache_dir).expanduser())
    if os.environ.get("AGENT_WEB_ALLOW_PRIVATE_NETWORKS") == "1":
        config = replace(config, allow_private_networks=True)
    if pandoc := os.environ.get("AGENT_WEB_PANDOC"):
        config = replace(config, pandoc=pandoc)
    return config


def load_config(path: Path | None = None) -> Config | InvalidConfig:
    path = path or config_path()
    if not path.exists():
        return apply_env(Config())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return InvalidConfig(f"can't read {path}: {e}")
    parsed = parse_config(data)
    if isinstance(parsed, InvalidConfig):
        return InvalidConfig(f"{path}: {parsed.message}")
    return apply_env(parsed)
