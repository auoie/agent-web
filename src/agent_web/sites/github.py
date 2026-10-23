"""GitHub file pages: fetch the raw file instead of the page around it.

A `github.com/<owner>/<repo>/blob/<ref>/<path>` page draws code one element per line, which loses
indentation in conversion. raw.githubusercontent.com serves the exact file.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from agent_web.urls import hostname

_BLOB_PATH = re.compile(r"^/([^/]+)/([^/]+)/blob/(.+)$")
# The note when the raw file wasn't found and the github.com page was read instead.
PAGE_FALLBACK = (
    "raw.githubusercontent.com answered 404 (a private repository's files need the github.com "
    "session, which it doesn't take), so this is the github.com page, where code can lose its "
    "indentation"
)


def raw_url(url: str) -> str | None:
    """The raw file URL for a GitHub file page, else None."""
    parts = urlsplit(url)
    if hostname(url) not in ("github.com", "www.github.com"):
        return None
    match = _BLOB_PATH.match(parts.path)
    if not match:
        return None
    owner, repo, ref_and_path = match.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_and_path}"
