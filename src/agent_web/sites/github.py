"""GitHub file pages: fetch the raw file instead of the page around it.

A `github.com/<owner>/<repo>/blob/<ref>/<path>` page draws code one element per line, which loses
indentation in conversion. raw.githubusercontent.com serves the exact file.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_BLOB_PATH = re.compile(r"^/([^/]+)/([^/]+)/blob/(.+)$")


def raw_url(url: str) -> str | None:
    """The raw file URL for a GitHub file page, else None."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    match = _BLOB_PATH.match(parts.path)
    if not match:
        return None
    owner, repo, ref_and_path = match.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_and_path}"
