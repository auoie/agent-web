"""Reading result URLs back out of `web search` text output (used by the hooks; no heavy imports).

`output.render_search` prints each result as a numbered title line followed by an indented URL
line; tests assert that the two stay in sync.
"""

from __future__ import annotations

import re

SEARCH_RESULT_URL = re.compile(r"^\d+\. .*\n   (https?://\S+)$", re.MULTILINE)


def urls_from_search_output(stdout: str) -> list[str]:
    return SEARCH_RESULT_URL.findall(stdout)
