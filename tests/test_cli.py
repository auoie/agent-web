from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest

from agent_web.cli import main
from agent_web.config import Config, parse_config
from agent_web.failures import Captcha, Failure
from agent_web.search.google import SearchResult, SearchResults
from conftest import Server, requires_pandoc


def run_cli(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def local_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_WEB_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setenv("AGENT_WEB_CACHE_DIR", str(tmp_path / "cli-cache"))


def test_version_and_help() -> None:
    assert run_cli("--version")[:2] == (0, "agent-web 0.1.0\n")
    code, out, _ = run_cli()
    assert code == 1
    assert "web search" in out


def test_bad_arguments() -> None:
    code, _, err = run_cli("fetch")
    assert code == 1
    assert err.startswith("web: ")


def test_config_example_is_valid() -> None:
    code, out, _ = run_cli("config", "--example")
    assert code == 0
    assert isinstance(parse_config(tomllib.loads(out)), Config)
    code, out, _ = run_cli("config")
    assert code == 0
    assert "browser = 'firefox'" in out


def test_invalid_config_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[nope]\n")
    monkeypatch.setenv("AGENT_WEB_CONFIG", str(path))
    code, _, err = run_cli("fetch", "https://example.com")
    assert code == 1
    assert "unknown section [nope]" in err


@requires_pandoc
@pytest.mark.usefixtures("local_ok")
def test_fetch_exit_codes(server: Server) -> None:
    code, out, _ = run_cli("fetch", server.url("/html"))
    assert code == 0
    assert "# Hello" in out
    assert run_cli("fetch", server.url("/notfound"))[0] == 3
    assert run_cli("fetch", server.url("/captcha"))[0] == 4
    code, _, err = run_cli("fetch", server.url("/slow"), "--refresh")
    assert code == 0 or "failed" in err


@requires_pandoc
@pytest.mark.usefixtures("local_ok")
def test_fetch_options(server: Server) -> None:
    code, out, _ = run_cli("fetch", server.url("/html"), "--max-chars", "5", "--offset", "2")
    assert code == 0
    assert "showing 2-7" in out
    code, out, _ = run_cli("fetch", server.url("/html"), "--find", "content", "--json")
    assert code == 0
    assert '"matches": 1' in out
    assert run_cli("fetch", server.url("/html"), "--max-chars", "0")[0] == 1


def test_fetch_failures() -> None:
    code, _, err = run_cli("fetch", "ftp://example.com/x")
    assert code == 1
    assert "only http" in err
    code, _, err = run_cli("fetch", "http://127.0.0.1:1/")
    assert code == 1
    assert "private network" in err


def test_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    from http.cookiejar import Cookie

    from agent_web.convert.pandoc import Pandoc
    from agent_web.failures import CookieReadFailure, ToolMissing
    from test_cookies import cookie

    jars: dict[str, list[Cookie] | CookieReadFailure] = {
        "ok": [cookie("SID", "x", ".google.com")],
        "anon": [cookie("NID", "x", ".google.com")],
        "broken": CookieReadFailure("firefox", "locked"),
    }
    state = {"jar": "ok", "pandoc": True}

    def loader(
        browser: str, domain: str, cookie_file: Path | None
    ) -> list[Cookie] | CookieReadFailure:
        return jars[str(state["jar"])]

    def pandoc(configured: str | None = None) -> Pandoc | ToolMissing:
        return (
            Pandoc("/usr/bin/pandoc", (3, 9))
            if state["pandoc"]
            else ToolMissing("pandoc", "get it")
        )

    monkeypatch.setattr("agent_web.cookies.load_browser_cookies", loader)
    monkeypatch.setattr("agent_web.convert.pandoc.find_pandoc", pandoc)
    code, out, _ = run_cli("doctor")
    assert code == 0
    assert "google:   signed in" in out
    assert "pandoc:   /usr/bin/pandoc (3.9)" in out
    state["jar"] = "anon"
    code, out, _ = run_cli("doctor")
    assert code == 1
    assert "NOT signed in" in out
    state["jar"], state["pandoc"] = "broken", False
    code, out, _ = run_cli("doctor")
    assert code == 1
    assert "can't read firefox cookies: locked" in out
    assert "MISSING: get it" in out


@pytest.mark.parametrize(
    ("args", "query", "n"),
    [
        (["vodvod", "-site:vodvod.top", "-n", "20"], "vodvod -site:vodvod.top", 20),
        (["vodvod.top", "reddit", "-n", "20"], "vodvod.top reddit", 20),
        (["vodvod.top", "safe", "OR", "scam", "-n15"], "vodvod.top safe OR scam", 15),
        (["vodvod.top", "down", "OR", "not working"], 'vodvod.top down OR "not working"', 10),
        (["pandoc lua filter", "-n", "5"], "pandoc lua filter", 5),
        (["-site:pinterest.com pasta"], "-site:pinterest.com pasta", 10),
        (["--json", "pics", "-nsfw"], "pics -nsfw", 10),
        (["-n", "3", "--", "-n", "x"], "-n x", 3),
    ],
)
def test_search_query_can_be_several_words(
    args: list[str], query: str, n: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int]] = []

    def fake_search(query: str, n: int, config: Config) -> SearchResults:
        seen.append((query, n))
        return SearchResults(query, ())

    monkeypatch.setattr("agent_web.search.google.search", fake_search)
    code, _, err = run_cli("search", *args)
    assert (code, err) == (0, "")
    assert seen == [(query, n)]


def test_search_without_a_query_is_a_usage_error() -> None:
    assert run_cli("search", "-n", "5")[0] == 1


def test_search_output_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_search(query: str, n: int, config: Config) -> SearchResults | Failure:
        if query == "blocked":
            return Captcha("Google")
        return SearchResults(query, (SearchResult("Title", "https://a.example/", "snippet"),))

    monkeypatch.setattr("agent_web.search.google.search", fake_search)
    code, out, _ = run_cli("search", "pandoc", "-n", "500")
    assert code == 0
    assert "1. Title\n   https://a.example/\n   snippet" in out
    code, out, _ = run_cli("search", "pandoc", "--json")
    assert '"url": "https://a.example/"' in out
    code, _, err = run_cli("search", "blocked")
    assert code == 4
    assert "captcha" in err
