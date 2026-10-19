from __future__ import annotations

import pytest

from agent_web.sites.github import raw_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/psf/requests/blob/main/src/requests/api.py",
            "https://raw.githubusercontent.com/psf/requests/main/src/requests/api.py",
        ),
        (
            "https://www.github.com/o/r/blob/feature/x/README.md#L10-L20",
            "https://raw.githubusercontent.com/o/r/feature/x/README.md",
        ),
        ("https://github.com/psf/requests", None),
        ("https://github.com/psf/requests/tree/main/src", None),
        ("https://github.com/psf/requests/issues/1", None),
        ("https://gitlab.com/o/r/blob/main/x.py", None),
    ],
)
def test_raw_url(url: str, expected: str | None) -> None:
    assert raw_url(url) == expected
