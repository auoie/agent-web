"""Which engine answers a search: Google, or Brave when Google can't (or for every search).

With a Brave key, a search Google refuses (paused, blocked, over the agents' budget, or not usable
at all) goes to Brave instead, and the results carry a note saying why. `--engine google` turns
that off for one search; search.engine = "brave" sends every search to Brave.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, assert_never

from agent_web.config import Config
from agent_web.failures import (
    SEARCH_PAUSE_MINUTES,
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    FallbackFailed,
    JavaScriptWall,
    NoApiKey,
    NotSignedIn,
    SearchBudgetUsed,
    SearchPaused,
)
from agent_web.search import SearchResult, SearchResults, brave, google

Engine = Literal["google", "brave"]
# The ways Google can refuse that another engine can make up for.
GoogleRefusal = (
    SearchPaused
    | Captcha
    | JavaScriptWall
    | SearchBudgetUsed
    | NotSignedIn
    | CookieReadFailure
    | ConsentRequired
)
GoogleSearch = Callable[..., SearchResults | Failure]
BraveSearch = Callable[..., list[SearchResult] | Failure]


def why_brave(refusal: GoogleRefusal) -> str:
    """The note on results Brave answered in Google's place."""
    paused = f"Google searches are paused for {SEARCH_PAUSE_MINUTES} minutes"
    match refusal:
        case SearchPaused(_, cause, minutes_left):
            reason = (
                f"Google searches are paused for {minutes_left} more minute(s) after it answered "
                f"one with {cause}"
            )
        case Captcha():
            reason = f"Google answered with a captcha ({paused})"
        case JavaScriptWall():
            reason = f"Google answered with its JavaScript check ({paused})"
        case SearchBudgetUsed(_, searches, _):
            reason = (
                f"the agents sent {searches} Google searches in the last 10 minutes, the most "
                "agent-web allows"
            )
        case NotSignedIn(_, browser):
            reason = f"Google can't be used: not signed in to Google in {browser}"
        case CookieReadFailure(browser, _):
            reason = f"Google can't be used: agent-web can't read {browser}'s cookies"
        case ConsentRequired():
            reason = "Google shows a consent page"
        case _:
            assert_never(refusal)
    return f"{reason}, so Brave answered this search"


def _brave(
    query: str, page: int, config: Config, key: str, brave_search: BraveSearch, note: str
) -> SearchResults | Failure:
    found = brave_search(query, page, config, key)
    if isinstance(found, list):
        return SearchResults(query, tuple(found), page, engine=brave.SERVICE, note=note)
    return found


def search(
    query: str,
    page: int,
    config: Config,
    engine: Engine | None = None,
    *,
    google_search: GoogleSearch = google.search,
    brave_search: BraveSearch = brave.search,
) -> SearchResults | Failure:
    """One results page from `engine`, or from the configured engine with Brave as the fallback."""
    key = config.brave_api_key
    if (engine or config.search_engine) == "brave":
        if key is None:
            return NoApiKey(brave.SERVICE)
        return _brave(query, page, config, key, brave_search, "")
    fallback = key is not None and engine is None
    # With Brave to fall back on, a search doesn't wait for a Google slot.
    budget_wait = 0.0 if fallback else google.BUDGET_WAIT_SECONDS
    outcome = google_search(query, page, config, budget_wait=budget_wait)
    if key is None or not fallback or not isinstance(outcome, GoogleRefusal):
        return outcome
    answer = _brave(query, page, config, key, brave_search, why_brave(outcome))
    return answer if isinstance(answer, SearchResults) else FallbackFailed(outcome, answer)
