from __future__ import annotations

import socket

import pytest

from agent_web.failures import InvalidUrl, NetworkFailure, PrivateAddress
from agent_web.urls import domain_matches, normalize, origin, public_addresses, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/a?b=c#d",
        "https://example.com/パス",
        "  https://x.io  ",
    ],
)
def test_validate_url_accepts_http_urls(url: str) -> None:
    assert validate_url(url) == url.strip()


def test_international_host_names_become_ascii() -> None:
    """The address check, the pinned connection and curl must all look up the same name."""
    assert validate_url("https://例え.jp/パス") == "https://xn--r8jz45g.jp/パス"
    assert validate_url("http://Bücher.de:8080/x") == "http://xn--bcher-kva.de:8080/x"
    assert validate_url("https://faß.de/") == "https://xn--fa-hia.de/"  # as browsers do (UTS 46)
    invalid = validate_url("https://a‍b.de/")  # a zero-width joiner where it isn't allowed
    assert isinstance(invalid, InvalidUrl)
    assert "international domain name" in invalid.reason


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
        "http://100.100.100.100/",  # carrier-grade NAT: Tailscale's address range
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",  # 127.0.0.1 written as one number
        "http://0.0.0.0/",
    ],
)
def test_public_addresses_refuses_local_addresses(url: str) -> None:
    assert isinstance(public_addresses(url), PrivateAddress)


def test_public_addresses_returns_the_checked_addresses() -> None:
    assert public_addresses("http://93.184.215.14/") == ("93.184.215.14",)


def test_one_private_address_among_public_ones_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str, port: int | None, **kwargs: int) -> list[tuple[object, ...]]:
        return [(2, 1, 6, "", (address, 0)) for address in ("93.184.215.14", "127.0.0.1")]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert public_addresses("http://mixed.example/") == PrivateAddress("mixed.example")


def test_unresolvable_host_is_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert isinstance(public_addresses("http://nowhere.example/"), NetworkFailure)


def test_origin_fills_in_the_default_port() -> None:
    assert origin("https://Api.Example/x") == ("https", "api.example", 443)
    assert origin("http://api.example/x") == ("http", "api.example", 80)  # not https's origin
    assert origin("https://api.example:8443/") == ("https", "api.example", 8443)
