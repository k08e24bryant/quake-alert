"""Helpers for webhook tests: a fixed Fernet key, fake DNS, and signature checks."""

import hashlib
import hmac
import json
import socket
from collections.abc import Mapping
from typing import Any

import httpx
import respx

from app.core.crypto import SecretBox
from app.notifications.ssrf import Resolver

# A valid Fernet key used only by tests (32 bytes, urlsafe base64).
FERNET_KEY = "dGVzdC1rZXktMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY="
OTHER_FERNET_KEY = "b3RoZXIta2V5LTAxMjM0NTY3ODlhYmNkZWYwMTIzNDU="

HOST = "hooks.example.com"
PUBLIC_IP = "93.184.216.34"
URL = f"https://{HOST}/quake"
IP_URL = f"https://{PUBLIC_IP}:443/quake"


def secret_box() -> SecretBox:
    return SecretBox([FERNET_KEY])


def resolver(answers: Mapping[str, list[str]] | None = None) -> Resolver:
    """Fake DNS: HOST -> PUBLIC_IP unless overridden; unknown names fail like NXDOMAIN."""
    table = {HOST: [PUBLIC_IP], **(answers or {})}

    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return table[host]

    return resolve


def expected_signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def posted(route: respx.Route) -> list[httpx.Request]:
    return [call.request for call in route.calls]


def payload(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body
