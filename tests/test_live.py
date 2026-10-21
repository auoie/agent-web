"""Live tests: the real internet and your browser's Google session. Run with `just test-live`.

They catch upstream drift (Google's page layout, pandoc and pymupdf behavior on real pages).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_web.config import Config, load_config
from agent_web.fetch import fetch
from agent_web.model import FetchResult, HtmlPage, PdfDocument, PlainText
from agent_web.search import brave
from agent_web.search.google import SearchResults, search
from conftest import REAL_BRAVE_KEY, REAL_HOME

pytestmark = pytest.mark.live


@pytest.fixture
def live_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("HOME", str(REAL_HOME))  # the real browser profile, for the refresh
    monkeypatch.setenv("XDG_CONFIG_HOME", str(REAL_HOME / ".config"))  # the real config
    if REAL_BRAVE_KEY:
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", REAL_BRAVE_KEY)
    config = load_config()
    assert isinstance(config, Config), config
    return replace(config, cache_dir=tmp_path / "cache")


def test_google_search(live_config: Config) -> None:
    outcome = search("pandoc lua filter", 1, live_config)
    assert isinstance(outcome, SearchResults), outcome
    assert len(outcome.results) >= 5
    assert all(r.url.startswith("http") and r.title for r in outcome.results)
    assert sum(bool(r.snippet) for r in outcome.results) >= 3


def test_brave_search(live_config: Config) -> None:
    if live_config.brave_api_key is None:
        pytest.skip("no Brave Search API key configured")
    found = brave.search(
        'sqlite "write-ahead log" -site:sqlite.org', 1, live_config, live_config.brave_api_key
    )
    assert isinstance(found, list), found
    assert len(found) >= 5
    assert all(r.url.startswith("http") and r.title for r in found)
    assert not any("sqlite.org" in r.url.split("/")[2] for r in found)


def test_wikipedia_merged_cells_survive(live_config: Config) -> None:
    result = fetch(
        "https://en.wikipedia.org/wiki/List_of_largest_companies_by_revenue", live_config
    )
    assert isinstance(result, FetchResult), result
    assert isinstance(result.content, HtmlPage)
    assert "rowspan=" in result.content.markdown
    assert "style=" not in result.content.markdown


def test_arxiv_pdf_keeps_exponents(live_config: Config) -> None:
    result = fetch("https://arxiv.org/pdf/1706.03762", live_config)
    assert isinstance(result, FetchResult), result
    content = result.content
    assert isinstance(content, PdfDocument)
    assert content.pages >= 10
    assert "10<sup>19</sup>" in content.markdown


def test_arxiv_html_keeps_formulas(live_config: Config) -> None:
    result = fetch("https://arxiv.org/html/1706.03762", live_config)
    assert isinstance(result, FetchResult), result
    assert isinstance(result.content, HtmlPage)
    assert r"\sqrt{d_{k}}" in result.content.markdown


def test_github_file_page_gives_the_exact_file(live_config: Config) -> None:
    result = fetch("https://github.com/psf/requests/blob/main/src/requests/api.py", live_config)
    assert isinstance(result, FetchResult), result
    assert isinstance(result.content, PlainText)
    assert "\n    method: str," in result.content.text  # indentation intact
