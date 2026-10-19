from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from agent_web.config import EXAMPLE_CONFIG, Config, load_config, parse_config
from agent_web.failures import InvalidConfig


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.toml")
    assert isinstance(config, Config)
    assert config.browser == "firefox"
    assert config.cookie_domains == ()
    assert not config.allow_private_networks


def test_example_config_is_valid() -> None:
    parsed = parse_config(tomllib.loads(EXAMPLE_CONFIG))
    assert isinstance(parsed, Config)


def test_full_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [cookies]
        browser = "Chrome"
        cookie_file = "~/cookies.sqlite"
        [search]
        min_interval_seconds = 5
        [fetch]
        cookie_domains = ["Reddit.com", ".news.example.org", "reddit.com"]
        trusted_domains = ["arXiv.org", "docs.python.org"]
        timeout_seconds = 12.5
        max_download_mb = 3
        allow_private_networks = true
        [output]
        max_chars = 9000
        [cache]
        dir = "/tmp/aw"
        ttl_seconds = 0
        max_mb = 10
        [convert]
        pandoc = "/opt/pandoc"
        """,
        encoding="utf-8",
    )
    config = load_config(path)
    assert isinstance(config, Config)
    assert config.browser == "chrome"
    assert config.cookie_file == Path("~/cookies.sqlite").expanduser()
    assert config.search_min_interval == 5
    assert config.cookie_domains == ("news.example.org", "reddit.com")
    assert config.trusted_domains == ("arxiv.org", "docs.python.org")
    assert config.timeout == 12.5
    assert config.max_download_bytes == 3 * 1024 * 1024
    assert config.allow_private_networks
    assert config.max_chars == 9000
    assert config.cache_dir == Path("/tmp/aw")
    assert config.cache_ttl == 0
    assert config.cache_max_bytes == 10 * 1024 * 1024
    assert config.pandoc == "/opt/pandoc"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"bogus": {}}, "unknown section [bogus]"),
        ({"fetch": {"cookie_domain": []}}, "unknown key fetch.cookie_domain"),
        ({"fetch": "x"}, "[fetch] must be a table"),
        ({"cookies": {"browser": "netscape"}}, "cookies.browser must be one of"),
        ({"cookies": {"browser": ""}}, "non-empty string"),
        ({"fetch": {"cookie_domains": "reddit.com"}}, "must be a list"),
        ({"fetch": {"cookie_domains": [1]}}, "must be a list of strings"),
        ({"fetch": {"cookie_domains": ["https://reddit.com"]}}, "not a bare domain"),
        (
            {"fetch": {"trusted_domains": ["arxiv.org/abs"]}},
            "fetch.trusted_domains: 'arxiv.org/abs'",
        ),
        ({"fetch": {"trusted_domains": "arxiv.org"}}, "fetch.trusted_domains must be a list"),
        ({"fetch": {"timeout_seconds": "5"}}, "must be a number"),
        ({"fetch": {"timeout_seconds": True}}, "must be a number"),
        ({"fetch": {"timeout_seconds": 0}}, "must be >= 1"),
        ({"fetch": {"allow_private_networks": "yes"}}, "true or false"),
        ({"output": {"max_chars": 10}}, "must be >= 500"),
    ],
)
def test_invalid_config(data: dict[str, Any], message: str) -> None:
    result = parse_config(data)
    assert isinstance(result, InvalidConfig)
    assert message in result.message


def test_unreadable_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[fetch\n", encoding="utf-8")
    result = load_config(path)
    assert isinstance(result, InvalidConfig)
    assert str(path) in result.message


def test_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WEB_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("AGENT_WEB_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setenv("AGENT_WEB_PANDOC", "/x/pandoc")
    config = load_config(tmp_path / "missing.toml")
    assert isinstance(config, Config)
    assert config.cache_dir == tmp_path / "c"
    assert config.allow_private_networks
    assert config.pandoc == "/x/pandoc"


def test_config_path_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom.toml"
    path.write_text("[output]\nmax_chars = 1234\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_WEB_CONFIG", str(path))
    config = load_config()
    assert isinstance(config, Config)
    assert config.max_chars == 1234
