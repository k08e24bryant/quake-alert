import pytest
from starlette.requests import Request

from app.core.rate_limit import RateLimitDecision, client_ip


def request(peer: str, forwarded_for: str | None = None) -> Request:
    headers = [] if forwarded_for is None else [(b"x-forwarded-for", forwarded_for.encode())]
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_peer_address_is_used_when_proxy_headers_are_not_trusted() -> None:
    assert client_ip(request("10.0.0.5", "203.0.113.7"), trust_proxy_headers=False) == "10.0.0.5"


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("198.51.100.1, 203.0.113.7", "203.0.113.7"),  # last entry: appended by our proxy
        ("2001:db8::1", "2001:db8::1"),
        ("not-an-ip", "10.0.0.5"),  # garbage falls back to the peer
        ("", "10.0.0.5"),
    ],
)
def test_trusted_forwarded_for_uses_the_last_entry(forwarded_for: str, expected: str) -> None:
    assert client_ip(request("10.0.0.5", forwarded_for), trust_proxy_headers=True) == expected


def test_retry_after_only_when_limited() -> None:
    allowed = RateLimitDecision(allowed=True, limit=60, remaining=59, reset_seconds=30)
    limited = RateLimitDecision(allowed=False, limit=60, remaining=0, reset_seconds=30)

    assert "Retry-After" not in allowed.headers()
    assert limited.headers() == {
        "X-RateLimit-Limit": "60",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "30",
        "Retry-After": "30",
    }
