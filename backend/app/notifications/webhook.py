"""The webhook channel: signed JSON POSTs to a subscriber's URL.

Every request:
  - goes to the address app.notifications.ssrf vetted just before, not to whatever DNS
    says at connect time (DNS rebinding), with the original Host header and TLS SNI, so
    the certificate is still verified for the subscriber's hostname;
  - follows no redirects, uses no proxy from the environment and no pooled connection;
  - times out after webhook_timeout_seconds and reads at most webhook_max_response_bytes
    of the response (raw, never decompressed);
  - is signed: X-Quake-Signature: sha256=HMAC_SHA256(secret, f"{timestamp}.{body}").

Status codes: 2xx sent; 410 gone (deactivate); other 3xx/4xx permanent; 5xx retry.
"""

import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.crypto import SecretBox, SecretBoxError, secret_box_from_settings
from app.notifications.base import (
    AlertData,
    PermanentNotifierError,
    Recipient,
    RecipientGoneError,
    RetryableNotifierError,
)
from app.notifications.messages import DISCLAIMER
from app.notifications.ssrf import (
    DnsResolutionError,
    Resolver,
    UnsafeTargetError,
    WebhookTarget,
    parse_target,
    resolve_public_address,
    system_resolver,
)
from app.schemas.earthquakes import Earthquake

SCHEMA_VERSION = 1
EVENT_ALERT = "earthquake.alert"
EVENT_SYNTHETIC = "earthquake.test"  # a dev-only synthetic quake, never a real one
EVENT_WEBHOOK_TEST = "webhook.test"  # POST /v1/subscriptions/webhook/{id}/test
POTENTIAL_LABEL = "Potensi (BMKG)"
SOURCE = {"notice": "Sumber: BMKG", "url": "https://www.bmkg.go.id"}

DELIVERY_ID_HEADER = "X-Quake-Delivery-Id"
TIMESTAMP_HEADER = "X-Quake-Timestamp"
SIGNATURE_HEADER = "X-Quake-Signature"
USER_AGENT = "quake-alert-webhook/1"

_WHITESPACE = re.compile(r"\s+")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def encode(payload: dict[str, Any]) -> bytes:
    """The exact bytes that are signed and sent."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _envelope(*, event: str, delivery_id: uuid.UUID, test: bool, synthetic: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "delivery_id": str(delivery_id),
        "test": test,
        "synthetic": synthetic,
        "possible_duplicate": False,
        "earthquake": None,
        "source": SOURCE,
        "disclaimer": DISCLAIMER,
    }


def alert_payload(alert: AlertData) -> dict[str, Any]:
    payload = _envelope(
        event=EVENT_SYNTHETIC if alert.is_synthetic else EVENT_ALERT,
        delivery_id=alert.delivery_id,
        test=False,
        synthetic=alert.is_synthetic,
    )
    # The same fields and formats as the public API, so one parser serves both.
    earthquake = Earthquake(
        id=alert.earthquake_id,
        occurred_at=alert.occurred_at,
        magnitude=float(alert.magnitude),
        depth_km=alert.depth_km,
        latitude=alert.latitude,
        longitude=alert.longitude,
        region=alert.region,
        potential=alert.potential,
        felt=alert.felt,
        shakemap_url=alert.shakemap_url,
        source_feeds=alert.source_feeds,
        distance_km=alert.distance_km,
    ).model_dump(mode="json")
    # BMKG's free text "Potensi", verbatim: labelled, never presented as tsunami info.
    earthquake["potential_label"] = POTENTIAL_LABEL
    payload["earthquake"] = earthquake
    payload["possible_duplicate"] = alert.possible_duplicate_of is not None
    return payload


def webhook_test_payload(delivery_id: uuid.UUID) -> dict[str, Any]:
    """For checking a receiver (connectivity and signature): no earthquake data at all."""
    return _envelope(event=EVENT_WEBHOOK_TEST, delivery_id=delivery_id, test=True, synthetic=False)


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status_code: int
    body: bytes  # at most max_response_bytes
    truncated: bool


class WebhookNotifier:
    """The webhook channel behind the common Notifier interface."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        secret_box: SecretBox | None,
        *,
        allow_http: bool,
        timeout_seconds: float,
        max_response_bytes: int,
        resolver: Resolver = system_resolver,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._box = secret_box
        self._allow_http = allow_http
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._resolver = resolver
        self._clock = clock

    @classmethod
    def from_settings(
        cls, http: httpx.AsyncClient, settings: Settings, resolver: Resolver = system_resolver
    ) -> "WebhookNotifier":
        return cls(
            http,
            secret_box_from_settings(settings),
            allow_http=settings.environment == "development",
            timeout_seconds=settings.webhook_timeout_seconds,
            max_response_bytes=settings.webhook_max_response_bytes,
            resolver=resolver,
        )

    @property
    def secret_box(self) -> SecretBox | None:
        return self._box

    async def check_url(self, url: str) -> WebhookTarget:
        """Subscription-time validation (the same rules as at send time, which is the
        check that matters). Raises UnsafeTargetError or DnsResolutionError."""
        target = parse_target(url, allow_http=self._allow_http)
        await resolve_public_address(target, self._resolver)
        return target

    async def send(self, recipient: Recipient, alert: AlertData) -> None:
        await self.post(recipient, alert_payload(alert), alert.delivery_id)

    async def send_test(self, recipient: Recipient) -> WebhookResponse:
        delivery_id = uuid.uuid4()
        return await self.post(recipient, webhook_test_payload(delivery_id), delivery_id)

    async def post(
        self, recipient: Recipient, payload: dict[str, Any], delivery_id: uuid.UUID
    ) -> WebhookResponse:
        if not recipient.webhook_url or not recipient.webhook_secret_encrypted:
            raise PermanentNotifierError("subscription has no webhook URL or secret")
        if self._box is None:
            raise PermanentNotifierError("WEBHOOK_SECRET_KEYS is not configured")
        try:
            secret = self._box.decrypt(recipient.webhook_secret_encrypted)
        except SecretBoxError:
            raise PermanentNotifierError(
                "signing secret cannot be decrypted (was a key removed from WEBHOOK_SECRET_KEYS?)"
            ) from None
        try:
            target = parse_target(recipient.webhook_url, allow_http=self._allow_http)
            address = await resolve_public_address(target, self._resolver)
        except UnsafeTargetError as exc:
            raise PermanentNotifierError(f"unsafe webhook target: {exc}") from None
        except DnsResolutionError as exc:
            raise RetryableNotifierError(str(exc)) from None

        body = encode(payload)
        timestamp = int(self._clock())
        headers = {
            "Host": target.host_header,
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            DELIVERY_ID_HEADER: str(delivery_id),
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign(secret, timestamp, body),
        }
        extensions = {} if target.is_ip_literal else {"sni_hostname": target.host}
        try:
            async with self._http.stream(
                "POST",
                target.url_for(address),
                content=body,
                headers=headers,
                timeout=self._timeout,
                extensions=extensions,
            ) as response:
                received, truncated = await self._read_limited(response)
        except httpx.TimeoutException:
            raise RetryableNotifierError("timeout") from None
        except httpx.TransportError as exc:
            raise RetryableNotifierError(f"connection failed: {type(exc).__name__}") from None
        return self._judge(WebhookResponse(response.status_code, received, truncated))

    async def _read_limited(self, response: httpx.Response) -> tuple[bytes, bool]:
        received = bytearray()
        async for chunk in response.aiter_raw():
            received.extend(chunk)
            if len(received) > self._max_response_bytes:
                return bytes(received[: self._max_response_bytes]), True
        return bytes(received), False

    def _judge(self, response: WebhookResponse) -> WebhookResponse:
        status = response.status_code
        if 200 <= status < 300:
            return response
        snippet = _WHITESPACE.sub(" ", response.body[:200].decode("utf-8", "replace")).strip()
        description = f"HTTP {status}" + (f": {snippet}" if snippet else "")
        if status == 410:
            raise RecipientGoneError(description)
        if 300 <= status < 400:
            raise PermanentNotifierError(f"HTTP {status}: redirect not followed")
        if status >= 500:
            raise RetryableNotifierError(description)
        raise PermanentNotifierError(description)


def create_webhook_http_client() -> httpx.AsyncClient:
    """No redirects, no environment proxies, no connection reuse: a pooled connection is
    keyed by IP, and must never carry a request for another hostname's TLS session."""
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
