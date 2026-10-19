from __future__ import annotations

import pytest

from agent_web.failures import InvalidUrl, PrivateAddress
from agent_web.urls import check_public, domain_matches, normalize, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/a?b=c#d",
        "https://例え.jp/パス",
        "  https://x.io  ",
    ],
)
def test_validate_url_accepts_http_urls(url: str) -> None:
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/file", "only http"),
        ("javascript:alert(1)", "only http"),
        ("https://", "no host"),
        ("https://user:pass@example.com/", "credentials"),
        ("https://example.com:99999/", "invalid port"),
        ("https://example.com/" + "a" * 9000, "longer than"),
    ],
)
def test_validate_url_rejects(url: str, reason: str) -> None:
    result = validate_url(url)
    assert isinstance(result, InvalidUrl)
    assert reason in result.reason


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("http://Example.com/Path/", "https://example.com/Path"),
        ("https://example.com:443/x#frag", "https://example.com/x"),
        ("https://example.com", "https://example.com/"),
        ("HTTPS://EXAMPLE.COM./a/", "https://example.com/a"),
    ],
)
def test_normalize_treats_equivalent_urls_as_equal(a: str, b: str) -> None:
    assert normalize(a) == normalize(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.com/a?x=1", "https://example.com/a?x=2"),
        ("https://example.com/A", "https://example.com/a"),
        ("https://example.com:8443/", "https://example.com/"),
        ("https://a.example.com/", "https://example.com/"),
    ],
)
def test_normalize_keeps_meaningful_differences(a: str, b: str) -> None:
    assert normalize(a) != normalize(b)


@pytest.mark.parametrize(
    ("host", "domain", "expected"),
    [
        ("reddit.com", "reddit.com", True),
        ("www.reddit.com", "reddit.com", True),
        ("old.reddit.com", ".reddit.com", True),
        ("notreddit.com", "reddit.com", False),
        ("reddit.com.evil.io", "reddit.com", False),
        ("reddit.com", "", False),
    ],
)
def test_domain_matches(host: str, domain: str, expected: bool) -> None:
    assert domain_matches(host, domain) is expected


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1:8080/",
        "http://10.1.2.3/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://app.localhost/",
    ],
)
def test_check_public_blocks_local_addresses(url: str) -> None:
    assert isinstance(check_public(url, allow_private=False), PrivateAddress)
    assert check_public(url, allow_private=True) is None


def test_check_public_allows_public_ip() -> None:
    assert check_public("http://93.184.215.14/", allow_private=False) is None
