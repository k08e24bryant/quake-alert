import ipaddress

import pytest

from app.notifications.ssrf import (
    DnsResolutionError,
    UnsafeTargetError,
    WebhookTarget,
    blocked_reason,
    parse_target,
    resolve_public_address,
)
from tests.webhook_samples import HOST, PUBLIC_IP, resolver

# --- URL rules ----------------------------------------------------------------------------


def test_parses_an_https_url() -> None:
    target = parse_target("https://Hooks.Example.com:8443/a/b?x=1#frag", allow_http=False)

    assert target == WebhookTarget("https", "hooks.example.com", 8443, "/a/b?x=1")
    assert target.host_header == "hooks.example.com:8443"
    assert target.url_for(ipaddress.ip_address(PUBLIC_IP)) == f"https://{PUBLIC_IP}:8443/a/b?x=1"


def test_default_port_is_left_out_of_the_host_header() -> None:
    target = parse_target("https://hooks.example.com", allow_http=False)

    assert (target.port, target.path, target.host_header) == (443, "/", "hooks.example.com")


def test_ipv6_target_is_bracketed() -> None:
    target = parse_target("https://[2606:2800:220:1:248:1893:25c8:1946]/x", allow_http=False)

    assert target.is_ip_literal
    assert target.host_header == "[2606:2800:220:1:248:1893:25c8:1946]"
    assert target.url_for(ipaddress.ip_address(target.host)).startswith("https://[2606:")


def test_internationalised_host_header_is_ascii() -> None:
    target = parse_target("https://bücher.example/hook", allow_http=False)

    assert target.host_header == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://hooks.example.com/x", "scheme"),
        ("ftp://hooks.example.com/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("gopher://hooks.example.com/", "scheme"),
        ("https://user:pass@hooks.example.com/", "credentials"),
        ("https://user@hooks.example.com/", "credentials"),
        ("https:///no-host", "no host"),
        ("https://hooks.example.com/a b", "whitespace"),
        ("https://hooks.example.com/\r\nX-Injected: 1", "whitespace"),
        ("https://hooks.example.com:99999/", "invalid URL"),
        ("https://" + "a" * 2050 + ".com/", "longer than"),
    ],
)
def test_rejected_urls(url: str, reason: str) -> None:
    with pytest.raises(UnsafeTargetError, match=reason):
        parse_target(url, allow_http=False)


def test_http_only_when_explicitly_allowed() -> None:
    assert parse_target("http://hooks.example.com/x", allow_http=True).port == 80
    with pytest.raises(UnsafeTargetError, match="scheme"):
        parse_target("ftp://hooks.example.com/x", allow_http=True)


# --- address rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("127.255.255.254", "loopback"),
        ("::1", "loopback"),
        ("10.1.2.3", "private"),
        ("172.16.0.1", "private"),
        ("192.168.1.1", "private"),
        ("fc00::1", "private"),
        ("fd12:3456::1", "private"),
        ("169.254.169.254", "cloud metadata"),
        ("169.254.170.2", "cloud metadata"),
        ("fd00:ec2::254", "cloud metadata"),
        ("100.100.100.200", "cloud metadata"),
        ("169.254.1.1", "link-local"),
        ("fe80::1", "link-local"),
        ("100.64.0.1", "carrier-grade NAT"),
        ("100.127.255.254", "carrier-grade NAT"),
        ("224.0.0.1", "multicast"),
        ("239.255.255.250", "multicast"),
        ("ff02::1", "multicast"),
        ("0.0.0.0", "unspecified"),  # noqa: S104
        ("::", "unspecified"),
        # Python counts these IANA special-purpose ranges as private.
        ("240.0.0.1", "private"),  # reserved for future use
        ("255.255.255.255", "private"),  # broadcast
        ("192.0.2.10", "private"),  # documentation
        ("198.18.0.1", "private"),  # benchmarking
        ("::ffff:127.0.0.1", "embeds 127.0.0.1"),
        ("::ffff:169.254.169.254", "embeds 169.254.169.254"),
        ("2002:7f00:1::", "embeds 127.0.0.1"),  # 6to4
        ("2002:a9fe:a9fe::", "embeds 169.254.169.254"),  # 6to4
        ("64:ff9b::7f00:1", "embeds 127.0.0.1"),  # NAT64
        ("64:ff9b::a00:1", "embeds 10.0.0.1"),  # NAT64
        # NAT64 to a public address is still refused: the prefix itself is reserved.
        ("64:ff9b::808:808", "reserved"),
    ],
)
def test_blocked_addresses(address: str, reason: str) -> None:
    result = blocked_reason(ipaddress.ip_address(address))

    assert result is not None
    assert reason in result


@pytest.mark.parametrize("address", [PUBLIC_IP, "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert blocked_reason(ipaddress.ip_address(address)) is None


# --- resolving ------------------------------------------------------------------------------


async def test_resolves_to_the_public_address() -> None:
    target = parse_target(f"https://{HOST}/x", allow_http=False)

    address = await resolve_public_address(target, resolver())

    assert str(address) == PUBLIC_IP


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        (["127.0.0.1"], "loopback"),
        (["169.254.169.254"], "cloud metadata"),
        (["::1"], "loopback"),
        (["10.0.0.5"], "private"),
        # One bad answer poisons the lot: a mixed answer is treated as hostile.
        ([PUBLIC_IP, "127.0.0.1"], "loopback"),
        (["fe80::1%eth0"], "link-local"),
    ],
)
async def test_name_resolving_to_a_blocked_address_is_rejected(
    answers: list[str], reason: str
) -> None:
    target = parse_target("https://evil.example/x", allow_http=False)

    with pytest.raises(UnsafeTargetError, match=reason):
        await resolve_public_address(target, resolver({"evil.example": answers}))


@pytest.mark.parametrize(
    "url", ["https://127.0.0.1/x", "https://[::1]/x", "https://169.254.169.254/latest/meta-data"]
)
async def test_ip_literal_urls_are_checked_without_dns(url: str) -> None:
    async def no_dns(host: str, port: int) -> list[str]:
        raise AssertionError("an IP literal must not be resolved")

    with pytest.raises(UnsafeTargetError):
        await resolve_public_address(parse_target(url, allow_http=False), no_dns)


async def test_unresolvable_name_is_a_retryable_dns_error() -> None:
    target = parse_target("https://does-not-exist.example/x", allow_http=False)

    with pytest.raises(DnsResolutionError):
        await resolve_public_address(target, resolver())


async def test_empty_dns_answer_is_a_dns_error() -> None:
    target = parse_target("https://empty.example/x", allow_http=False)

    with pytest.raises(DnsResolutionError):
        await resolve_public_address(target, resolver({"empty.example": []}))
