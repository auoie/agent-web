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
# Search APIs that can answer when Google can't, with the environment variable for each one's key,
# in the order they're tried by default (search/apis.py has the rest of each one's details).
API_KEY_ENV = {
    "brave": "BRAVE_SEARCH_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "parallel": "PARALLEL_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "langsearch": "LANGSEARCH_API_KEY",
    "serper": "SERPER_API_KEY",
}
ENGINES = ("google", *API_KEY_ENV)
# Search APIs that also answer without a key (capped per IP address), when search.keyless is on.
KEYLESS_APIS = frozenset({"firecrawl"})

EXAMPLE_CONFIG = """\
# agent-web configuration. Every key is optional.

[cookies]
# Browser whose cookies are used: firefox, librewolf, chrome, chromium, brave, vivaldi, edge, opera
browser = "firefox"
# cookie_file = "~/.mozilla/firefox/xxxxxxxx.default-release/cookies.sqlite"

[search]
# Minimum seconds between Google requests (protects your account from rate limits)
min_interval_seconds = 2.0
# When Google answers with its JavaScript check, run the check in a headless Firefox or LibreWolf
# (with a throwaway copy of your Google cookies) and retry, instead of pausing searches
browser_refresh = true
# Google requests allowed per 10 minutes across all agents (0: no limit). Google started blocking
# agents' searches after 25-38 in 10 minutes; past this limit, searches go to the search APIs
# below (if a key is set) or wait their turn
google_searches_per_10_minutes = 20
# Which engine answers first: google, brave, serpapi, tavily, exa, parallel, firecrawl, langsearch
# or serper. When it can't, the search APIs with a key answer, in this order
engine = "google"
# fallbacks = ["brave", "serpapi", "tavily", "exa", "parallel", "firecrawl", "langsearch", "serper"]
# Also use search APIs that answer without a key (Firecrawl: a daily cap per IP address); they see
# the queries they answer. Search APIs listed in `fallbacks` are used keyless too.
keyless = false

[keys]
# Search API keys (`web doctor` lists where to get them). Each has an environment variable that
# takes precedence: BRAVE_SEARCH_API_KEY, SERPAPI_API_KEY, TAVILY_API_KEY, EXA_API_KEY,
# PARALLEL_API_KEY, FIRECRAWL_API_KEY, LANGSEARCH_API_KEY, SERPER_API_KEY.
# If you put keys here, make this file private (chmod 600)
# brave = "..."

[fetch]
# Domains that receive your browser cookies when fetched (subdomains included).
# Every other site gets an anonymous request.
cookie_domains = []
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
    browser_refresh: bool = True
    google_budget: int = 20
    search_engine: str = "google"
    # Search APIs to try when the first engine can't answer; None: every API that can be asked
    # (usable()), in API_KEY_ENV's order.
    fallbacks: tuple[str, ...] | None = None
    keyless: bool = False
    api_keys: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    cookie_domains: tuple[str, ...] = ()
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
    "search": frozenset(
        {
            "min_interval_seconds",
            "browser_refresh",
            "google_searches_per_10_minutes",
            "engine",
            "fallbacks",
            "keyless",
        }
    ),
    "keys": frozenset(API_KEY_ENV),
    "fetch": frozenset(
        {"cookie_domains", "timeout_seconds", "max_download_mb", "allow_private_networks"}
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


def _bool(table: Mapping[str, Any], key: str, where: str) -> bool | None:
    if key not in table:
        return None
    value: object = table[key]
    if not isinstance(value, bool):
        raise _Invalid(f"{where}.{key} must be true or false")
    return value


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


def _names(table: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    """search.fallbacks: a list of search API names."""
    if key not in table:
        return None
    value: object = table[key]
    if not isinstance(value, list):
        raise _Invalid(f"search.{key} must be a list of names")
    names: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, str) or item.lower() not in API_KEY_ENV:
            raise _Invalid(f"search.{key}: {item!r} isn't one of: {', '.join(API_KEY_ENV)}")
        names.append(item.lower())
    return tuple(dict.fromkeys(names))


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
    if (refresh := _bool(search, "browser_refresh", "search")) is not None:
        config = replace(config, browser_refresh=refresh)
    budget = _number(search, "google_searches_per_10_minutes", "search", 0)
    if budget is not None:
        config = replace(config, google_budget=int(budget))
    if (engine := _string(search, "engine", "search")) is not None:
        if engine.lower() not in ENGINES:
            raise _Invalid(f"search.engine must be one of: {', '.join(ENGINES)}")
        config = replace(config, search_engine=engine.lower())
    if (fallbacks := _names(search, "fallbacks")) is not None:
        config = replace(config, fallbacks=fallbacks)
    if (keyless := _bool(search, "keyless", "search")) is not None:
        config = replace(config, keyless=keyless)

    keys = _table(data, "keys")
    api_keys = {name: key for name in API_KEY_ENV if (key := _string(keys, name, "keys"))}
    config = replace(config, api_keys=api_keys)

    fetch = _table(data, "fetch")
    if (cookie_domains := _domains(fetch, "cookie_domains")) is not None:
        config = replace(config, cookie_domains=cookie_domains)
    if (timeout := _number(fetch, "timeout_seconds", "fetch", 1)) is not None:
        config = replace(config, timeout=timeout)
    if (max_mb := _number(fetch, "max_download_mb", "fetch", 1)) is not None:
        config = replace(config, max_download_bytes=int(max_mb * 1024 * 1024))
    if (allow := _bool(fetch, "allow_private_networks", "fetch")) is not None:
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
    from_env = {
        name: key for name, env in API_KEY_ENV.items() if (key := os.environ.get(env, "").strip())
    }
    if from_env:
        config = replace(config, api_keys={**config.api_keys, **from_env})
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


def usable(config: Config, name: str) -> bool:
    """Whether search API `name` can be asked: it has a key, or answers without one and the
    user allowed that (search.keyless, or by listing it in search.fallbacks)."""
    listed = config.fallbacks is not None and name in config.fallbacks
    return name in config.api_keys or (name in KEYLESS_APIS and (config.keyless or listed))


def fallback_order(config: Config) -> tuple[str, ...]:
    """The search APIs to try after the first engine, in order: those that can be asked."""
    order = config.fallbacks if config.fallbacks is not None else tuple(API_KEY_ENV)
    return tuple(n for n in order if usable(config, n) and n != config.search_engine)
