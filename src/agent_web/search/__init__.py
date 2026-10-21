"""Web search backends: Google (search/google.py) and Brave (search/brave.py), which answers when
Google can't or when configured to answer every search. search/dispatch.py picks between them."""

from __future__ import annotations

from dataclasses import dataclass

# How deep `web search --page` goes. Each page is one request.
MAX_PAGE = 5
RESULTS_PER_PAGE = 10


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchResults:
    query: str
    results: tuple[SearchResult, ...]
    page: int = 1
    engine: str = "Google"
    note: str = ""  # why another engine than the configured one answered
