"""Which engine answers: Google, the search APIs in turn when it can't, or one engine only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_web.config import Config
from agent_web.failures import (
    EXIT_BLOCKED,
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    JavaScriptWall,
    NetworkFailure,
    NoApiKey,
    NoEngineAnswered,
    NotSignedIn,
    SearchApiError,
    SearchBudgetUsed,
    SearchPaused,
    describe,
    exit_code,
)
from agent_web.search import SearchResult, SearchResults
from agent_web.search.apis import Api
from agent_web.search.dispatch import search, setback
from agent_web.search.ledger import Ledger

HIT = SearchResult("T", "https://a.example/", "s")
NOW = datetime(2026, 10, 21, 22, 30, tzinfo=UTC).timestamp()
OUT_OF_CREDIT = SearchApiError("Brave", 402, "Payment required")


class Engines:
    """Fake Google and search APIs that record how they were called."""

    def __init__(
        self, google: SearchResults | Failure, apis: dict[str, list[SearchResult] | Failure]
    ) -> None:
        self.google_answer = google
        self.api_answers = apis
        self.google_waits: list[float] = []
        self.asked: list[str] = []

    def google(
        self, query: str, page: int, config: Config, *, budget_wait: float
    ) -> SearchResults | Failure:
        self.google_waits.append(budget_wait)
        return self.google_answer

    def api(
        self, api: Api, query: str, page: int, config: Config, key: str
    ) -> list[SearchResult] | Failure:
        assert key in (f"{api.name}-key", "")  # "": a keyless API asked without a key
        self.asked.append(api.name)
        return self.api_answers.get(api.name, [HIT])

    def run(
        self, config: Config, ledger: Ledger, engine: str | None = None, page: int = 1
    ) -> SearchResults | Failure:
        return search(
            "q",
            page,
            config,
            engine,
            google_search=self.google,
            api_search=self.api,
            ledger=ledger,
            now=lambda: NOW,
        )


def keyed(config: Config, *names: str) -> Config:
    return replace(config, api_keys={name: f"{name}-key" for name in names})


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.json", now=lambda: NOW)


def test_google_answers_when_it_can(config: Config, ledger: Ledger) -> None:
    engines = Engines(SearchResults("q", (HIT,), 1), {})
    outcome = engines.run(keyed(config, "brave"), ledger)
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Google"
    assert engines.google_waits == [0.0]  # with an API to fall back on, no waiting for Google
    assert engines.asked == []


@pytest.mark.parametrize(
    ("refusal", "why"),
    [
        (
            SearchPaused("Google", "a captcha", 7),
            "Google searches are paused for 7 more minute(s) after it answered one with a captcha",
        ),
        (
            Captcha("Google"),
            "Google answered with a captcha (Google searches are paused for 10 minutes)",
        ),
        (
            JavaScriptWall("Google"),
            "Google answered with its JavaScript check (Google searches are paused for 10 minutes)",
        ),
        (
            SearchBudgetUsed("Google", 20, 95),
            "the agents sent 20 Google searches in the last 10 minutes, the most agent-web allows",
        ),
        (
            NotSignedIn("Google", "firefox"),
            "Google can't be used: not signed in to Google in firefox",
        ),
        (
            CookieReadFailure("firefox", "locked"),
            "Google can't be used: agent-web can't read firefox's cookies",
        ),
        (ConsentRequired("Google"), "Google shows a consent page"),
    ],
)
def test_an_api_answers_when_google_refuses(
    config: Config, ledger: Ledger, refusal: Failure, why: str
) -> None:
    outcome = Engines(refusal, {}).run(keyed(config, "brave"), ledger)
    assert outcome == SearchResults(
        "q", (HIT,), 1, engine="Brave", note=f"{why}, so Brave answered this search"
    )
    assert ledger.used("brave") == 1


def test_the_apis_are_tried_in_order_and_failures_are_skipped_for_a_while(
    config: Config, ledger: Ledger
) -> None:
    config = keyed(config, "brave", "tavily", "exa")
    rejected = SearchApiError("Tavily", 401, "")
    engines = Engines(Captcha("Google"), {"brave": OUT_OF_CREDIT, "tavily": rejected})
    outcome = engines.run(config, ledger)
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Exa"
    assert outcome.max_page == 2
    assert outcome.note == (
        "Google answered with a captcha (Google searches are paused for 10 minutes); Brave is "
        "out of credit; Tavily rejected the API key, so Exa answered this search"
    )
    assert engines.asked == ["brave", "tavily", "exa"]
    # The next search skips them without asking: Brave until the next UTC day, Tavily for an hour.
    engines.asked.clear()
    outcome = engines.run(config, ledger)
    assert engines.asked == ["exa"]
    assert isinstance(outcome, SearchResults)
    assert "Brave is out of credit (skipped until 00:00 UTC)" in outcome.note
    assert "Tavily rejected the API key (skipped until 23:30 UTC)" in outcome.note


def test_order_comes_from_the_config(config: Config, ledger: Ledger) -> None:
    config = replace(keyed(config, "brave", "tavily"), fallbacks=("tavily", "brave"))
    engines = Engines(Captcha("Google"), {})
    assert isinstance(engines.run(config, ledger), SearchResults)
    assert engines.asked == ["tavily"]


def test_pages_an_api_cant_give_go_to_the_next_one(config: Config, ledger: Ledger) -> None:
    engines = Engines(Captcha("Google"), {})
    outcome = engines.run(keyed(config, "parallel", "serper"), ledger, page=2)
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Serper"
    assert "Parallel gives only 1 page of results" in outcome.note


def test_when_no_api_can_answer_every_reason_is_given(config: Config, ledger: Ledger) -> None:
    paused = SearchPaused("Google", "a captcha", 7)
    offline = NetworkFailure("https://api.tavily.com/search", "timeout")
    outcome = Engines(paused, {"brave": OUT_OF_CREDIT, "tavily": offline}).run(
        keyed(config, "brave", "tavily"), ledger
    )
    assert isinstance(outcome, NoEngineAnswered)
    assert outcome.primary == paused
    assert exit_code(outcome) == EXIT_BLOCKED
    message = describe(outcome)
    assert "paused for 7 more minute(s)" in message
    assert (
        "Brave is out of credit; Tavily failed (request to https://api.tavily.com/search "
        "failed: timeout)"
    ) in message


def test_without_keys_google_waits_for_budget_and_reports_refusals(
    config: Config, ledger: Ledger
) -> None:
    paused = SearchPaused("Google", "a captcha", 7)
    engines = Engines(paused, {})
    assert engines.run(config, ledger) == paused
    assert engines.google_waits == [30.0]


def test_other_google_failures_are_returned(config: Config, ledger: Ledger) -> None:
    offline = NetworkFailure("https://www.google.com/search", "timeout")
    engines = Engines(offline, {})
    assert engines.run(keyed(config, "brave"), ledger) == offline
    assert engines.asked == []


def test_engine_names_one_engine_only(config: Config, ledger: Ledger) -> None:
    config = keyed(config, "brave", "tavily")
    engines = Engines(Captcha("Google"), {"tavily": OUT_OF_CREDIT})
    assert engines.run(config, ledger, "google") == Captcha("Google")
    assert engines.google_waits == [30.0]
    assert engines.run(config, ledger, "brave") == SearchResults("q", (HIT,), 1, engine="Brave")
    assert engines.run(config, ledger, "tavily") == NoEngineAnswered(
        None, (("Tavily", "is out of credit"),)
    )
    assert engines.run(config, ledger, "exa") == NoApiKey("exa", "EXA_API_KEY")
    assert engines.asked == ["brave", "tavily"]


def test_a_first_engine_other_than_google(config: Config, ledger: Ledger) -> None:
    config = replace(keyed(config, "brave", "tavily"), search_engine="tavily")
    engines = Engines(Captcha("Google"), {"tavily": OUT_OF_CREDIT})
    outcome = engines.run(config, ledger)
    assert outcome == SearchResults(
        "q",
        (HIT,),
        1,
        engine="Brave",
        note="Tavily is out of credit, so Brave answered this search",
    )
    assert engines.google_waits == []


def test_setback() -> None:
    assert setback(SearchApiError("Brave", 402, ""), NOW)[0] == "is out of credit"
    assert setback(SearchApiError("X", 429, "monthly quota exceeded"), NOW)[0] == "is out of credit"
    assert setback(SearchApiError("X", 429, "slow down"), NOW) == ("is limiting requests", NOW + 60)
    assert setback(SearchApiError("X", 403, ""), NOW) == ("rejected the API key", NOW + 3600)
    assert setback(SearchApiError("X", 500, "oops"), NOW) == ("failed (HTTP 500: oops)", NOW + 60)


def test_ledger_counts_by_month(tmp_path: Path) -> None:
    clock = [NOW]
    ledger = Ledger(tmp_path / "l.json", now=lambda: clock[0])
    ledger.count("brave")
    ledger.count("brave")
    ledger.skip("tavily", NOW + 60, "is limiting requests")
    assert (ledger.used("brave"), ledger.used("tavily")) == (2, 0)
    assert ledger.skipped("tavily") is not None
    clock[0] = NOW + 61
    assert ledger.skipped("tavily") is None
    clock[0] = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    assert ledger.used("brave") == 0  # a new month
    ledger.count("brave")
    assert ledger.used("brave") == 1
    (tmp_path / "l.json").write_text("not json")
    assert Ledger(tmp_path / "l.json").used("brave") == 0


def test_429_wording_decides_between_out_of_credit_and_rate_limited() -> None:
    brave_burst = SearchApiError("Brave", 429, "Request rate limit exceeded")
    assert setback(brave_burst, NOW) == ("is limiting requests", NOW + 60)
    asked_to_wait = SearchApiError("Tavily", 429, "Too many requests", retry_after=20.0)
    assert setback(asked_to_wait, NOW) == ("is limiting requests", NOW + 20)
    for detail in (
        "Your account has run out of searches.",
        "Daily token limit exceeded",
        "You have exceeded your monthly quota",
    ):
        assert setback(SearchApiError("X", 429, detail), NOW)[0] == "is out of credit"
    assert (
        setback(SearchApiError("Serper", 400, "Not enough credits"), NOW)[0] == "is out of credit"
    )


def test_each_api_is_paced_under_its_rate_limit(config: Config, ledger: Ledger) -> None:
    config = keyed(config, "firecrawl", "serper")  # Firecrawl: 10 searches a minute
    sleeps: list[float] = []
    engines = Engines(Captcha("Google"), {})
    for _ in range(10):
        outcome = search(
            "q",
            1,
            config,
            google_search=engines.google,
            api_search=engines.api,
            ledger=ledger,
            now=lambda: NOW,
            sleep=sleeps.append,
        )
        assert isinstance(outcome, SearchResults)
        assert outcome.engine == "Firecrawl"
    outcome = search(
        "q",
        1,
        config,
        google_search=engines.google,
        api_search=engines.api,
        ledger=ledger,
        now=lambda: NOW,
        sleep=sleeps.append,
    )
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Serper"
    assert "Firecrawl is at its limit of 10 a minute (next in 60 s)" in outcome.note
    assert sleeps == []


def test_ledger_claims_slots_in_a_rolling_window(tmp_path: Path) -> None:
    clock = [NOW]
    ledger = Ledger(tmp_path / "l.json", now=lambda: clock[0])
    assert ledger.claim("exa", 2, 1.0, 3.0) == (True, 0.0)
    assert ledger.claim("exa", 2, 1.0, 3.0) == (True, 0.0)
    assert ledger.claim("exa", 2, 1.0, 3.0) == (True, 1.0)  # reserved: send in 1 s
    assert ledger.claim("exa", 2, 1.0, 0.5) == (False, 1.0)  # the next slot is too far off
    clock[0] += 1.5  # only the reserved request (sent at +1 s) is still in the window
    assert ledger.claim("exa", 2, 1.0, 3.0) == (True, 0.0)
    assert ledger.claim("exa", 2, 1.0, 3.0) == (True, 0.5)  # the +1 s one leaves at +2 s


def test_keyless_firecrawl_only_when_allowed(config: Config, ledger: Ledger) -> None:
    engines = Engines(Captcha("Google"), {})
    assert engines.run(config, ledger) == Captcha("Google")  # nothing set up: no third party
    outcome = engines.run(replace(config, keyless=True), ledger)
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Firecrawl"
    listed = replace(config, fallbacks=("firecrawl",))
    assert isinstance(engines.run(listed, ledger), SearchResults)
    assert isinstance(engines.run(config, ledger, "firecrawl"), SearchResults)  # named: allowed
    assert engines.asked == ["firecrawl", "firecrawl", "firecrawl"]


def test_a_named_engine_is_asked_even_while_skipped(config: Config, ledger: Ledger) -> None:
    ledger.skip("exa", NOW + 3600, "is out of credit")
    engines = Engines(Captcha("Google"), {})
    outcome = engines.run(keyed(config, "exa"), ledger, "exa")
    assert outcome == SearchResults("q", (HIT,), 1, engine="Exa", max_page=2)
    assert ledger.skipped("exa") is None  # answering clears the skip


def test_operator_queries_go_to_engines_that_understand_them(
    config: Config, ledger: Ledger
) -> None:
    config = replace(keyed(config, "tavily", "exa"), keyless=True)  # Firecrawl comes later
    engines = Engines(Captcha("Google"), {})
    outcome = search(
        '"new grad" -senior',
        1,
        config,
        google_search=engines.google,
        api_search=engines.api,
        ledger=ledger,
        now=lambda: NOW,
    )
    assert isinstance(outcome, SearchResults)
    assert outcome.engine == "Firecrawl"  # quotes and -word: Firecrawl reads both
    assert engines.asked == ["firecrawl"]


def test_the_note_says_which_operators_an_engine_left_out(config: Config, ledger: Ledger) -> None:
    engines = Engines(Captcha("Google"), {})
    outcome = search(
        'rust "borrow checker" -beginner',
        1,
        keyed(config, "exa"),
        google_search=engines.google,
        api_search=engines.api,
        ledger=ledger,
        now=lambda: NOW,
    )
    assert isinstance(outcome, SearchResults)
    assert outcome.note.endswith(
        "so Exa answered this search. Exa doesn't support quoted phrases or -word exclusions, so "
        "results may not match them"
    )
