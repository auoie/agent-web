from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest

from agent_web.cli import main
from agent_web.config import Config, parse_config
from agent_web.failures import Captcha, Failure
from agent_web.search.google import SearchResult, SearchResults, limiter_for
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


@requires_pandoc
@pytest.mark.usefixtures("local_ok")
def test_fetch_options(server: Server) -> None:
    code, out, _ = run_cli("fetch", server.url("/html"), "--max-chars", "5", "--offset", "2")
    assert code == 0
    assert "showing 2-7" in out
    code, out, _ = run_cli("fetch", server.url("/html"), "--find", "content", "--json")
    assert code == 0
    assert '"matches": 1' in out
    assert run_cli("fetch", server.url("/html"), "--max-chars", "-1")[0] == 1
    code, out, _ = run_cli("fetch", server.url("/html"), "--max-chars", "0")
    assert code == 0
    assert "Full text: " in out
    assert "# Hello" not in out


@requires_pandoc
@pytest.mark.usefixtures("local_ok")
def test_fetch_path_prints_only_the_saved_file(server: Server) -> None:
    code, out, err = run_cli("fetch", server.url("/html"), "--path")
    assert (code, err) == (0, "")
    text = Path(out.strip())
    assert out == f"{text}\n"
    assert text.suffix == ".md"
    assert "# Hello" in text.read_text(encoding="utf-8")
    code, out, _ = run_cli("fetch", server.url("/html"), "--original-path")
    assert code == 0
    assert Path(out.strip()).read_bytes().startswith(b"<!doctype html>")
    # Notes still reach the agent, on stderr, and the exit code is unchanged.
    code, out, err = run_cli("fetch", server.url("/notfound"), "--path")
    assert code == 3
    assert Path(out.strip()).is_file()
    assert err.startswith("Note: the server answered HTTP 404")
    code, out, err = run_cli("fetch", server.url("/binary"), "--path")
    assert code == 0
    assert out.strip().endswith(".png")
    assert "a binary file has no text" in err


@requires_pandoc
@pytest.mark.usefixtures("local_ok")
@pytest.mark.parametrize(
    "extra", [["--find", "x"], ["--offset", "5"], ["--max-chars", "0"], ["--json"]]
)
def test_fetch_path_takes_no_output_options(server: Server, extra: list[str]) -> None:
    code, out, err = run_cli("fetch", server.url("/html"), "--path", *extra)
    assert (code, out) == (1, "")
    assert err.startswith("web: ")


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
    state["jar"], state["pandoc"] = "ok", True
    limiter_for(Config()).pause("its JavaScript check", 300)
    code, out, _ = run_cli("doctor")
    assert code == 1
    assert (
        "searches paused for 5 more minute(s): Google answered a search with its JavaScript" in out
    )
    assert "google:   searches in the last 10 minutes: 0 of 20 allowed" in out
    assert "search:   no key for: brave, serpapi, tavily" in out


def test_doctor_and_config_with_a_brave_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from http.cookiejar import Cookie

    from agent_web.convert.pandoc import Pandoc
    from test_cookies import cookie

    def loader(*args: object) -> list[Cookie]:
        return [cookie("SID", "x", ".google.com")]

    def pandoc(*args: object) -> Pandoc:
        return Pandoc("/usr/bin/pandoc", (3, 9))

    monkeypatch.setattr("agent_web.cookies.load_browser_cookies", loader)
    monkeypatch.setattr("agent_web.convert.pandoc.find_pandoc", pandoc)
    path = tmp_path / "config" / "agent-web" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[keys]\nbrave = "SECRET-KEY"\n', encoding="utf-8")
    path.chmod(0o644)
    code, out, _ = run_cli("doctor")
    assert code == 1
    assert (
        "brave:    key from the config file; 0 searches this month; free tier: $5 of credit" in out
    )
    assert "holds API keys and others can read it: chmod 600" in out
    assert (
        "search:   no key for: serpapi, tavily, exa, parallel, firecrawl, langsearch, serper" in out
    )
    path.chmod(0o600)
    code, out, _ = run_cli("doctor")
    assert code == 0
    monkeypatch.setenv("TAVILY_API_KEY", "ENV-KEY")
    code, out, _ = run_cli("doctor")
    assert "tavily:   key from TAVILY_API_KEY; 0 searches this month" in out
    path.write_text('[search]\nkeyless = true\n[keys]\nbrave = "SECRET-KEY"\n', encoding="utf-8")
    code, out, _ = run_cli("doctor")
    assert "firecrawl: no key: asked keyless; 0 searches this month" in out
    path.write_text(
        '[search]\nfallbacks = ["tavily"]\nengine = "exa"\n[keys]\nbrave = "SECRET-KEY"\n',
        encoding="utf-8",
    )
    code, out, _ = run_cli("doctor")
    assert code == 1
    assert "search:   NO API KEY for exa, the first engine (search.engine)" in out
    assert "exa:" not in out.replace("for exa,", "")  # not also listed as usable
    assert "search:   keys not used (not in search.fallbacks): brave" in out
    assert "no key for: serpapi, exa, parallel, firecrawl, langsearch, serper" in out
    path.chmod(0o644)  # an unused key in the file is still a secret
    code, out, _ = run_cli("doctor")
    assert "holds API keys and others can read it: chmod 600" in out
    code, out, _ = run_cli("config")
    assert "api_keys = ['brave', 'tavily']" in out
    assert "SECRET" not in out
    assert "ENV-KEY" not in out


def test_split_search_query_explains_quoting() -> None:
    code, _, err = run_cli("search", "sqlite", "-site:sqlite.org", "--page", "2")
    assert code == 1
    assert "unrecognized arguments: -site:sqlite.org" in err
    assert "Put all of it in single quotes" in err
    code, _, err = run_cli("search", "sqlite", "-n", "10")
    assert code == 1
    assert "-n was replaced by --page N" in err
    assert "single quotes" not in err
    _, _, fetch_err = run_cli("fetch", "https://a.example/", "extra")
    assert "single quotes" not in fetch_err


def test_search_output_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    engines: list[str | None] = []

    def fake_search(
        query: str, page: int, config: Config, engine: str | None = None
    ) -> SearchResults | Failure:
        asked.append(page)
        engines.append(engine)
        if query == "blocked":
            return Captcha("Google")
        return SearchResults(query, (SearchResult("Title", "https://a.example/", "snippet"),))

    monkeypatch.setattr("agent_web.search.dispatch.search", fake_search)
    code, out, _ = run_cli("search", "pandoc")
    assert code == 0
    assert "1. Title\n   https://a.example/\n   snippet" in out
    assert run_cli("search", "pandoc", "--page", "5", "--engine", "brave")[0] == 0
    assert asked == [1, 5]
    assert engines == [None, "brave"]
    code, _, err = run_cli("search", "pandoc", "--engine", "bing")
    assert code == 1
    assert "invalid choice: 'bing'" in err
    for bad in ("0", "6"):
        code, _, err = run_cli("search", "pandoc", "--page", bad)
        assert code == 1
        assert "--page must be between 1 and 5" in err
    code, out, _ = run_cli("search", "pandoc", "--json")
    assert '"url": "https://a.example/"' in out
    code, _, err = run_cli("search", "blocked")
    assert code == 4
    assert "captcha" in err


def test_doctor_shows_how_long_ago_a_refresh_was() -> None:
    from agent_web.cli import _refresh_line  # pyright: ignore[reportPrivateUsage]

    line = "2026-10-21 20:35:54 UTC  headless firefox, 1.3 s; the retry got a captcha"
    now = 1792614954.0 + 38 * 60  # 2026-10-21 20:35:54 UTC, plus 38 minutes
    assert _refresh_line(line, now) == (
        "past refresh, 38 min ago (20:35 UTC): headless firefox, 1.3 s; the retry got a captcha"
    )
    assert _refresh_line(line, now + 3 * 3600).startswith("past refresh, 3 h ago")
    assert _refresh_line("garbage", now) == "past refresh: garbage"
