"""SSRF protection for webhook targets.

A webhook URL is chosen by whoever creates the subscription, and the worker POSTs to it
from inside our network. So before every send:

1. the URL must be https (http only with ENVIRONMENT=development), with a host, no
   credentials and no control characters;
2. the host is resolved NOW (not trusted from subscription time) and EVERY address it
   resolves to must be publicly routable: no loopback, private, link-local (which includes
   the 169.254.169.254 cloud metadata service), carrier-grade NAT, multicast, reserved or
   otherwise non-global range, IPv4 or IPv6, including IPv4 hidden inside IPv6 forms
   (::ffff:127.0.0.1, 6to4, Teredo, NAT64);
3. the caller then connects to that exact address (see app.notifications.webhook), so a
   second DNS answer can't swap in a private address between check and connect (DNS
   rebinding).

There is deliberately no allowlist or bypass, in any environment.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

MAX_URL_LENGTH = 2048

_CLOUD_METADATA = {
    ipaddress.ip_address("169.254.169.254"),  # AWS, GCP, Azure, DigitalOcean, ...
    ipaddress.ip_address("169.254.170.2"),  # AWS ECS task metadata
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
    ipaddress.ip_address("192.0.0.192"),  # Oracle Cloud
}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeTargetError(Exception):
    """The URL or an address it resolves to may not be called. Never retried."""


class DnsResolutionError(Exception):
    """The host could not be resolved right now. Worth retrying."""


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    scheme: str
    host: str  # lowercase, without IPv6 brackets
    port: int
    path: str  # path and query, as given (already percent-encoded)

    @property
    def host_header(self) -> str:
        host = _ascii_host(self.host)
        if ":" in host:
            host = f"[{host}]"
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"

    @property
    def is_ip_literal(self) -> bool:
        return _as_ip(self.host) is not None

    def url_for(self, address: IPAddress) -> str:
        host = f"[{address}]" if address.version == 6 else str(address)
        return f"{self.scheme}://{host}:{self.port}{self.path}"


def parse_target(url: str, *, allow_http: bool) -> WebhookTarget:
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeTargetError(f"URL longer than {MAX_URL_LENGTH} characters")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise UnsafeTargetError("URL contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeTargetError(f"invalid URL: {exc}") from None
    scheme = parts.scheme.lower()
    allowed = {"https", "http"} if allow_http else {"https"}
    if scheme not in allowed:
        raise UnsafeTargetError(f"scheme must be {' or '.join(sorted(allowed))}")
    if parts.username is not None or parts.password is not None:
        raise UnsafeTargetError("credentials in the URL are not allowed")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsafeTargetError("URL has no host")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return WebhookTarget(scheme, host, port or (443 if scheme == "https" else 80), path)


def blocked_reason(address: IPAddress) -> str | None:
    """Why this address may not be called, or None if it is publicly routable."""
    if address in _CLOUD_METADATA:
        return "cloud metadata service"
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour]
        if address.teredo is not None:
            embedded.extend(address.teredo)
        if address in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        for ipv4 in embedded:
            if ipv4 is not None and (reason := blocked_reason(ipv4)) is not None:
                return f"embeds {ipv4} ({reason})"
    checks = (
        (address.is_loopback, "loopback"),
        (address.is_link_local, "link-local"),
        (address.is_multicast, "multicast"),
        (address.is_unspecified, "unspecified"),
        (address.version == 4 and address in _CGNAT, "carrier-grade NAT"),
        (address.is_private, "private"),
        (address.is_reserved, "reserved"),
        (not address.is_global, "not globally routable"),
    )
    return next((reason for blocked, reason in checks if blocked), None)


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def resolve_public_address(target: WebhookTarget, resolver: Resolver) -> IPAddress:
    """The address to connect to. Raises UnsafeTargetError if ANY resolved address is
    blocked (a mixed answer is treated as hostile), DnsResolutionError if none resolves."""
    literal = _as_ip(target.host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            answers = await resolver(target.host, target.port)
        except (OSError, UnicodeError) as exc:
            raise DnsResolutionError(f"cannot resolve {target.host}: {exc}") from None
        addresses = []
        for answer in answers:
            address = _as_ip(answer.split("%", 1)[0])  # drop an IPv6 zone id
            if address is None:
                raise UnsafeTargetError(f"{target.host} resolved to a non-IP answer")
            addresses.append(address)
        if not addresses:
            raise DnsResolutionError(f"{target.host} has no addresses")
    for address in addresses:
        if (reason := blocked_reason(address)) is not None:
            raise UnsafeTargetError(f"{target.host} resolves to {address} ({reason})")
    return addresses[0]


def _as_ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _ascii_host(host: str) -> str:
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return host.encode("idna").decode("ascii")
    return host
