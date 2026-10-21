"""Which engine answers: Google, Brave in Google's place, or Brave only."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_web.config import Config
from agent_web.failures import (
    EXIT_BLOCKED,
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    FallbackFailed,
    JavaScriptWall,
    NetworkFailure,
    NoApiKey,
    NotSignedIn,
    SearchApiError,
    SearchBudgetUsed,
    SearchPaused,
    describe,
    exit_code,
)
from agent_web.search import SearchResult, SearchResults
from agent_web.search.dispatch import Engine, search

HIT = SearchResult("T", "https://a.example/", "s")


class Engines:
    """Fake Google and Brave that record how they were called."""

    def __init__(
        self, google: SearchResults | Failure, brave: list[SearchResult] | Failure
    ) -> None:
        self.google_answer = google
        self.brave_answer = brave
        self.google_waits: list[float] = []
        self.brave_calls = 0

    def google(
        self, query: str, page: int, config: Config, *, budget_wait: float
    ) -> SearchResults | Failure:
        self.google_waits.append(budget_wait)
        return self.google_answer

    def brave(
        self, query: str, page: int, config: Config, key: str
    ) -> list[SearchResult] | Failure:
        assert key == "KEY"
        self.brave_calls += 1
        return self.brave_answer

    def run(self, config: Config, engine: Engine | None = None) -> SearchResults | Failure:
        return search("q", 2, config, engine, google_search=self.google, brave_search=self.brave)


@pytest.fixture
def keyed(config: Config) -> Config:
    return replace(config, brave_api_key="KEY")


def test_google_answers_when_it_can(keyed: Config) -> None:
    engines = Engines(SearchResults("q", (HIT,), 2), [HIT])
    outcome = engines.run(keyed)
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Google"
    assert engines.google_waits == [0.0]  # with Brave to fall back on, no waiting for Google
    assert engines.brave_calls == 0


@pytest.mark.parametrize(
    ("refusal", "note"),
    [
        (
            SearchPaused("Google", "a captcha", 7),
            "Google searches are paused for 7 more minute(s) after it answered one with a "
            "captcha, so Brave answered this search",
        ),
        (
            Captcha("Google"),
            "Google answered with a captcha (Google searches are paused for 10 minutes), so "
            "Brave answered this search",
        ),
        (
            SearchBudgetUsed("Google", 20, 95),
            "the agents sent 20 Google searches in the last 10 minutes, the most agent-web "
            "allows, so Brave answered this search",
        ),
        (
            JavaScriptWall("Google"),
            "Google answered with its JavaScript check (Google searches are paused for 10 "
            "minutes), so Brave answered this search",
        ),
        (
            CookieReadFailure("firefox", "locked"),
            "Google can't be used: agent-web can't read firefox's cookies, so Brave answered "
            "this search",
        ),
        (ConsentRequired("Google"), "Google shows a consent page, so Brave answered this search"),
        (
            NotSignedIn("Google", "firefox"),
            "Google can't be used: not signed in to Google in firefox, so Brave answered this "
            "search",
        ),
    ],
)
def test_brave_answers_when_google_refuses(keyed: Config, refusal: Failure, note: str) -> None:
    engines = Engines(refusal, [HIT])
    outcome = engines.run(keyed)
    assert outcome == SearchResults("q", (HIT,), 2, engine="Brave", note=note)


def test_other_google_failures_are_returned(keyed: Config) -> None:
    offline = NetworkFailure("https://www.google.com/search", "timeout")
    engines = Engines(offline, [HIT])
    assert engines.run(keyed) == offline
    assert engines.brave_calls == 0


def test_when_brave_fails_too_both_reasons_are_given(keyed: Config) -> None:
    paused = SearchPaused("Google", "a captcha", 7)
    out_of_credit = SearchApiError("Brave", 402, "Payment required")
    outcome = Engines(paused, out_of_credit).run(keyed)
    assert outcome == FallbackFailed(paused, out_of_credit)
    assert isinstance(outcome, FallbackFailed)
    assert exit_code(outcome) == EXIT_BLOCKED
    message = describe(outcome)
    assert "paused for 7 more minute(s)" in message
    assert "Brave couldn't answer instead: the Brave Search API answered HTTP 402" in message


def test_without_a_key_google_waits_for_budget_and_reports_refusals(config: Config) -> None:
    paused = SearchPaused("Google", "a captcha", 7)
    engines = Engines(paused, [HIT])
    assert engines.run(config) == paused
    assert engines.google_waits == [30.0]
    assert engines.brave_calls == 0


def test_engine_google_means_google_only(keyed: Config) -> None:
    paused = SearchPaused("Google", "a captcha", 7)
    engines = Engines(paused, [HIT])
    assert engines.run(keyed, "google") == paused
    assert engines.google_waits == [30.0]
    assert engines.brave_calls == 0


def test_engine_brave_means_brave_only(keyed: Config, config: Config) -> None:
    engines = Engines(SearchResults("q", (HIT,), 2), [HIT])
    assert engines.run(keyed, "brave") == SearchResults("q", (HIT,), 2, engine="Brave")
    assert engines.run(replace(keyed, search_engine="brave")) == SearchResults(
        "q", (HIT,), 2, engine="Brave"
    )
    assert engines.google_waits == []
    assert engines.run(config, "brave") == NoApiKey("Brave")
