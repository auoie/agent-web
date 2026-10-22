"""Which engine answers a search: Google first, then the search APIs with a key, in order.

A search Google refuses (paused, blocked, over the agents' budget, or not usable at all) goes to
the search APIs (search/apis.py) in turn until one answers, and the results carry a note saying
why. An API that is out of credit, rejected its key or is limiting requests is skipped for a while
(search/ledger.py) instead of being asked on every search. `--engine NAME` asks one engine only;
search.engine sets which engine is asked first.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import assert_never

from agent_web.config import API_KEY_ENV, KEYLESS_APIS, Config, fallback_order, usable
from agent_web.failures import (
    SEARCH_PAUSE_MINUTES,
    Captcha,
    ConsentRequired,
    CookieReadFailure,
    Failure,
    JavaScriptWall,
    NoApiKey,
    NoEngineAnswered,
    NotSignedIn,
    SearchApiError,
    SearchBudgetUsed,
    SearchPaused,
    describe,
)
from agent_web.search import SearchResult, SearchResults, apis, google
from agent_web.search.apis import APIS, OPERATORS, Api, operators_used
from agent_web.search.ledger import Ledger, ledger_for

# The ways Google can refuse that a search API can make up for.
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
ApiSearch = Callable[[Api, str, int, Config, str], list[SearchResult] | Failure]

# How long an API that couldn't answer is skipped (one out of credit: until the next UTC day).
KEY_REJECTED_SECONDS = 3600
SHORT_SKIP_SECONDS = 60
# A search waits this long at most for a slot under an API's rate limit, else asks the next one.
MAX_PACE_WAIT = 3.0
_OUT_OF_CREDIT = re.compile(
    r"credit|quota|usage limit|plan's|run out|ran out|insufficient|payment|upgrade|daily|per day|"
    r"monthly|per month|\bTPD\b",
    re.IGNORECASE,
)
# "Request rate limit exceeded" (Brave's per-second limit) is not "out of credit".
_RATE_LIMITED = re.compile(
    r"rate.?limit|too many requests|per second|per minute|\bRPS\b|\bTPM\b|concurren", re.IGNORECASE
)


def why_google(refusal: GoogleRefusal) -> str:
    """Why Google didn't answer, for the note on results a search API gave instead."""
    paused = f"Google searches are paused for {SEARCH_PAUSE_MINUTES} minutes"
    match refusal:
        case SearchPaused(_, cause, minutes_left):
            return (
                f"Google searches are paused for {minutes_left} more minute(s) after it answered "
                f"one with {cause}"
            )
        case Captcha():
            return f"Google answered with a captcha ({paused})"
        case JavaScriptWall():
            return f"Google answered with its JavaScript check ({paused})"
        case SearchBudgetUsed(_, searches, _):
            return (
                f"the agents sent {searches} Google searches in the last 10 minutes, the most "
                "agent-web allows"
            )
        case NotSignedIn(_, browser):
            return f"Google can't be used: not signed in to Google in {browser}"
        case CookieReadFailure(browser, _):
            return f"Google can't be used: agent-web can't read {browser}'s cookies"
        case ConsentRequired():
            return "Google shows a consent page"
        case _:
            assert_never(refusal)


def setback(failure: Failure, now: float) -> tuple[str, float]:
    """Why a search API couldn't answer, and until when to skip it."""
    if not isinstance(failure, SearchApiError):
        return f"failed ({describe(failure)})", now + SHORT_SKIP_SECONDS
    status, detail = failure.status, failure.detail
    if (
        failure.used_up
        or status in (402, 432, 433)
        or (_OUT_OF_CREDIT.search(detail) and not _RATE_LIMITED.search(detail))
    ):
        tomorrow = datetime.fromtimestamp(now, UTC).date() + timedelta(days=1)
        midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)
        return "is out of credit", midnight.timestamp()
    if status in (401, 403, 422):
        return "rejected the API key", now + KEY_REJECTED_SECONDS
    if status == 429:
        return "is limiting requests", now + (failure.retry_after or SHORT_SKIP_SECONDS)
    return f"failed (HTTP {status}{': ' + detail if detail else ''})", now + SHORT_SKIP_SECONDS


def _clock(timestamp: float) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(timestamp))


def search(
    query: str,
    page: int,
    config: Config,
    engine: str | None = None,
    *,
    google_search: GoogleSearch = google.search,
    api_search: ApiSearch = apis.search,
    ledger: Ledger | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchResults | Failure:
    """One results page from `engine` only, or from the first engine that can answer."""
    if engine == "google":
        return google_search(query, page, config, budget_wait=google.BUDGET_WAIT_SECONDS)
    primary: Failure | None = None
    reasons: list[str] = []
    if engine is not None:
        chain = [engine]
    elif config.search_engine != "google":
        chain = [config.search_engine, *fallback_order(config)]
    else:
        chain = list(fallback_order(config))
        # With search APIs to fall back on, a search doesn't wait for a Google slot.
        budget_wait = 0.0 if chain else google.BUDGET_WAIT_SECONDS
        outcome = google_search(query, page, config, budget_wait=budget_wait)
        if isinstance(outcome, SearchResults) or not chain:
            return outcome
        if not isinstance(outcome, GoogleRefusal):
            return outcome
        primary = outcome
        reasons.append(why_google(outcome))

    ledger = ledger or ledger_for(config)
    needs = operators_used(query)
    if engine is None and needs:
        # Engines that understand the query's operators first (search.engine stays first).
        start = 0 if config.search_engine == "google" else 1
        rest = sorted(chain[start:], key=lambda name: not needs <= APIS[name].operators)
        chain = [*chain[:start], *rest]
    tried: list[tuple[str, str]] = []
    for name in chain:
        api = APIS[name]
        # Naming a keyless API with --engine allows it without a key.
        if not usable(config, name) and not (engine is not None and name in KEYLESS_APIS):
            if engine is not None:
                return NoApiKey(name, API_KEY_ENV[name])
            tried.append((api.title, "has no API key"))
            continue
        key = config.api_keys.get(name, "")  # "": asked without a key
        if page > api.max_page:
            pages = "1 page" if api.max_page == 1 else f"{api.max_page} pages"
            tried.append((api.title, f"gives only {pages} of results"))
            continue
        # A named engine is asked even while skipped (e.g. to check it after topping up).
        if engine is None and (skipped := ledger.skipped(name)) is not None:
            tried.append((api.title, f"{skipped.why} (skipped until {_clock(skipped.until)})"))
            continue
        claimed, wait = ledger.claim(name, *api.rate, MAX_PACE_WAIT)
        if not claimed:
            tried.append((api.title, f"is at its limit of {api.rate_text} (next in {wait:.0f} s)"))
            continue
        if wait:
            sleep(wait)
        found = api_search(api, query, page, config, key)
        if isinstance(found, list):
            ledger.count(name)
            because = [*reasons, *(f"{title} {why}" for title, why in tried)]
            note = f"{'; '.join(because)}, so {api.title} answered this search" if because else ""
            if missing := [kind for kind in OPERATORS if kind in needs - api.operators]:
                caveat = f"{api.title} doesn't support {' or '.join(missing)}, so results may "
                caveat += "not match them"
                note = f"{note}. {caveat}" if note else caveat
            return SearchResults(
                query, tuple(found), page, engine=api.title, note=note, max_page=api.max_page
            )
        why, until = setback(found, now())
        ledger.skip(name, until, why)
        tried.append((api.title, why))
    return NoEngineAnswered(primary, tuple(tried))
