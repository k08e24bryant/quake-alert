"""Webhook deliveries through the shared pipeline (dispatcher.deliver), against mocked HTTP
(respx) and mocked DNS. Nothing here talks to the network."""

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DeliveryStatus, NotificationDelivery, Subscription
from app.notifications.dispatcher import (
    DeliveryOutcome,
    NotifyConfig,
    RetryDeliveryError,
    deliver,
)
from app.notifications.ssrf import Resolver
from app.notifications.telegram import TelegramClient
from app.notifications.webhook import WebhookNotifier, create_webhook_http_client
from app.notifications.webhook_subscriptions import recipient_of
from tests.integration.seed import BOGOR, JAKARTA, seed_quake, seed_webhook_subscription
from tests.webhook_samples import (
    HOST,
    IP_URL,
    PUBLIC_IP,
    URL,
    expected_signature,
    payload,
    posted,
    resolver,
    secret_box,
)

SECRET = "whsec_delivery-test"  # noqa: S105  (fake)
CLOCK = 1_790_851_492  # the notifier's "now" (Unix seconds)
CONFIG = NotifyConfig(
    max_age=timedelta(minutes=30),
    max_attempts=5,
    retry_backoff_seconds=5,
    duplicate_window=timedelta(seconds=120),
    duplicate_distance_m=100_000,
    max_consecutive_failures=3,
    allow_synthetic=True,
)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def webhook_http() -> AsyncIterator[httpx.AsyncClient]:
    client = create_webhook_http_client()
    yield client
    await client.aclose()


def notifier(
    http: httpx.AsyncClient, dns: Resolver | None = None, *, allow_http: bool = False
) -> WebhookNotifier:
    return WebhookNotifier(
        http,
        secret_box(),
        allow_http=allow_http,
        timeout_seconds=5,
        max_response_bytes=65536,
        max_retry_after_seconds=300,
        resolver=dns or resolver(),
        clock=lambda: CLOCK,
    )


async def new_delivery(
    session: AsyncSession,
    *,
    url: str = URL,
    subscription: Subscription | None = None,
    occurred_at: datetime | None = None,
    is_synthetic: bool = False,
) -> NotificationDelivery:
    if subscription is None:
        subscription = await seed_webhook_subscription(
            session, url=url, encrypted_secret=secret_box().encrypt(SECRET), at=JAKARTA
        )
    quake = await seed_quake(
        session,
        at=BOGOR,
        occurred_at=occurred_at or now() - timedelta(minutes=2),
        magnitude="5.4",
        potential="Tidak berpotensi tsunami",
        is_synthetic=is_synthetic,
    )
    delivery = NotificationDelivery(subscription_id=subscription.id, earthquake_id=quake.id)
    session.add(delivery)
    await session.flush()
    return delivery


async def run(
    ctx: dict[str, Any],
    webhook: WebhookNotifier,
    delivery: NotificationDelivery,
    *,
    attempt: int = 1,
    at: datetime | None = None,
    config: NotifyConfig = CONFIG,
) -> DeliveryOutcome:
    telegram: TelegramClient = ctx["telegram"]
    factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    return await deliver(
        factory,
        telegram,
        delivery.id,
        attempt=attempt,
        now=at or datetime.now(UTC),
        config=config,
        webhook=webhook,
    )


async def state(session: AsyncSession, delivery: NotificationDelivery) -> tuple[Any, ...]:
    row = (
        await session.execute(
            select(
                NotificationDelivery.status,
                NotificationDelivery.attempts,
                NotificationDelivery.last_error,
            ).where(NotificationDelivery.id == delivery.id)
        )
    ).one()
    return tuple(row)


async def subscription_state(session: AsyncSession, subscription_id: uuid.UUID) -> tuple[Any, ...]:
    row = (
        await session.execute(
            select(Subscription.is_active, Subscription.consecutive_failures).where(
                Subscription.id == subscription_id
            )
        )
    ).one()
    return tuple(row)


# --- what is sent ---------------------------------------------------------------------------


async def test_signed_alert_goes_to_the_vetted_ip_with_original_host_and_sni(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL).respond(204)
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, notifier(webhook_http), delivery)

    assert outcome is DeliveryOutcome.SENT
    [request] = posted(route)
    # Connected to the address resolved and checked a moment ago, not to a fresh lookup...
    assert request.url.host == PUBLIC_IP
    # ...while the receiver still sees its own hostname, and TLS verifies it.
    assert request.headers["host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    # Signature over f"{timestamp}.{body}" with the subscription's secret.
    timestamp = request.headers["x-quake-timestamp"]
    assert timestamp == "1790851492"
    assert request.headers["x-quake-signature"] == expected_signature(
        SECRET, timestamp, request.content
    )
    assert request.headers["x-quake-delivery-id"] == str(delivery.id)
    assert request.headers["content-type"] == "application/json"
    body = payload(request)
    assert (body["schema_version"], body["event"], body["delivery_id"]) == (
        1,
        "earthquake.alert",
        str(delivery.id),
    )
    assert body["earthquake"]["magnitude"] == 5.4
    assert body["earthquake"]["potential"] == "Tidak berpotensi tsunami"
    assert body["earthquake"]["potential_label"] == "Potensi (BMKG)"
    assert body["earthquake"]["distance_km"] == pytest.approx(43.2, abs=0.2)  # PostGIS
    assert (body["synthetic"], body["test"], body["possible_duplicate"]) == (False, False, False)
    assert await state(db_session, delivery) == (DeliveryStatus.SENT, 1, None)


async def test_synthetic_quake_is_sent_as_earthquake_test(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL).respond(200)
    delivery = await new_delivery(db_session, is_synthetic=True)

    await run(worker_ctx, notifier(webhook_http), delivery)

    body = payload(posted(route)[0])
    assert (body["event"], body["synthetic"]) == ("earthquake.test", True)


async def test_synthetic_quake_is_never_sent_outside_development(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL).respond(200)
    delivery = await new_delivery(db_session, is_synthetic=True)
    production = replace(CONFIG, allow_synthetic=False)

    outcome = await run(worker_ctx, notifier(webhook_http), delivery, config=production)

    assert outcome is DeliveryOutcome.FAILED
    assert not route.called


# --- status codes ---------------------------------------------------------------------------


async def test_5xx_retries_with_backoff_then_succeeds(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL)
    route.side_effect = [httpx.Response(503, text="busy"), httpx.Response(200)]
    delivery = await new_delivery(db_session)
    webhook = notifier(webhook_http)

    with pytest.raises(RetryDeliveryError) as caught:
        await run(worker_ctx, webhook, delivery, attempt=1)
    assert caught.value.defer_seconds == 5
    assert await state(db_session, delivery) == (DeliveryStatus.PENDING, 1, "HTTP 503: busy")

    assert await run(worker_ctx, webhook, delivery, attempt=2) is DeliveryOutcome.SENT
    first, second = posted(route)
    # Same delivery id on the retry, so the receiver can deduplicate.
    assert first.headers["x-quake-delivery-id"] == second.headers["x-quake-delivery-id"]
    assert await state(db_session, delivery) == (DeliveryStatus.SENT, 2, None)


@pytest.mark.parametrize(("attempt", "defer"), [(1, 5), (2, 10), (3, 20), (4, 40)])
async def test_backoff_is_exponential(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    attempt: int,
    defer: float,
) -> None:
    respx_mock.post(IP_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    delivery = await new_delivery(db_session)

    with pytest.raises(RetryDeliveryError) as caught:
        await run(worker_ctx, notifier(webhook_http), delivery, attempt=attempt)

    assert caught.value.defer_seconds == defer


async def test_fifth_attempt_failing_gives_up(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(IP_URL).respond(500)
    delivery = await new_delivery(db_session)

    assert await run(worker_ctx, notifier(webhook_http), delivery, attempt=5) is (
        DeliveryOutcome.FAILED
    )
    assert (await state(db_session, delivery))[0] is DeliveryStatus.FAILED


async def test_freshness_is_rechecked_before_a_retry(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL).respond(503)
    occurred_at = now() - timedelta(minutes=29)
    delivery = await new_delivery(db_session, occurred_at=occurred_at)
    webhook = notifier(webhook_http)

    with pytest.raises(RetryDeliveryError):
        await run(worker_ctx, webhook, delivery, attempt=1)
    # The retry runs after the quake passed the 30-minute window: dropped, not sent late.
    later = occurred_at + timedelta(minutes=31)
    outcome = await run(worker_ctx, webhook, delivery, attempt=2, at=later)

    assert outcome is DeliveryOutcome.EXPIRED
    assert route.call_count == 1
    assert await state(db_session, delivery) == (
        DeliveryStatus.FAILED,
        1,
        "expired before sending",
    )


async def test_410_deactivates_without_retry(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(IP_URL).respond(410)
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, notifier(webhook_http), delivery)  # no RetryDeliveryError

    assert outcome is DeliveryOutcome.DEACTIVATED
    assert (await subscription_state(db_session, delivery.subscription_id))[0] is False
    assert await state(db_session, delivery) == (DeliveryStatus.FAILED, 1, "HTTP 410")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_other_4xx_fail_without_retry(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    status: int,
) -> None:
    respx_mock.post(IP_URL).respond(status, text="nope")
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, notifier(webhook_http), delivery)

    assert outcome is DeliveryOutcome.FAILED
    assert await state(db_session, delivery) == (DeliveryStatus.FAILED, 1, f"HTTP {status}: nope")
    assert await subscription_state(db_session, delivery.subscription_id) == (True, 1)


# --- 429: retry after Retry-After ----------------------------------------------------------


def http_date(unix_seconds: float) -> str:
    return formatdate(unix_seconds, usegmt=True)  # e.g. "Thu, 01 Oct 2026 10:44:52 GMT"


@pytest.mark.parametrize(
    ("retry_after", "defer"),
    [
        ("120", 120),  # delay-seconds
        (http_date(CLOCK + 90), 90),  # HTTP date
        ("3600", 300),  # capped at WEBHOOK_MAX_RETRY_AFTER_SECONDS
        (http_date(CLOCK + 86_400), 300),  # capped, as a date too
        ("0", 0),
        (http_date(CLOCK - 60), 0),  # a date already past: retry now
        (None, 5),  # no header: the usual backoff
        ("soon", 5),  # unparseable: the usual backoff
    ],
)
async def test_429_is_retried_after_retry_after(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    retry_after: str | None,
    defer: float,
) -> None:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    respx_mock.post(IP_URL).respond(429, headers=headers, text="slow down")
    delivery = await new_delivery(db_session)

    with pytest.raises(RetryDeliveryError) as caught:
        await run(worker_ctx, notifier(webhook_http), delivery, attempt=1)

    assert caught.value.defer_seconds == defer
    # Still an attempt; and asking us to slow down is not a failure of the endpoint.
    assert await state(db_session, delivery) == (
        DeliveryStatus.PENDING,
        1,
        "rate limited: HTTP 429: slow down",
    )
    assert await subscription_state(db_session, delivery.subscription_id) == (True, 0)


async def test_429_then_success(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL)
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    delivery = await new_delivery(db_session)
    webhook = notifier(webhook_http)

    with pytest.raises(RetryDeliveryError):
        await run(worker_ctx, webhook, delivery, attempt=1)

    assert await run(worker_ctx, webhook, delivery, attempt=2) is DeliveryOutcome.SENT
    assert await state(db_session, delivery) == (DeliveryStatus.SENT, 2, None)


@pytest.mark.parametrize(("status", "counted"), [(429, 0), (503, 1)])
async def test_429_on_the_last_attempt_fails_without_counting_towards_deactivation(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    status: int,
    counted: int,
) -> None:
    respx_mock.post(IP_URL).respond(status, headers={"Retry-After": "10"})
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, notifier(webhook_http), delivery, attempt=5)

    assert outcome is DeliveryOutcome.FAILED
    assert (await state(db_session, delivery))[:2] == (DeliveryStatus.FAILED, 1)
    assert await subscription_state(db_session, delivery.subscription_id) == (True, counted)


@pytest.mark.parametrize(("status", "counted"), [(429, 0), (503, 1)])
async def test_429_still_respects_the_freshness_window(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    status: int,
    counted: int,
) -> None:
    route = respx_mock.post(IP_URL).respond(status, headers={"Retry-After": "300"})
    occurred_at = now() - timedelta(minutes=27)
    delivery = await new_delivery(db_session, occurred_at=occurred_at)
    webhook = notifier(webhook_http)

    with pytest.raises(RetryDeliveryError):
        await run(worker_ctx, webhook, delivery, attempt=1)
    # The receiver's wait (300 s) took the quake past the 30-minute window: dropped.
    later = occurred_at + timedelta(minutes=32)
    outcome = await run(worker_ctx, webhook, delivery, attempt=2, at=later)

    assert outcome is DeliveryOutcome.EXPIRED
    assert route.call_count == 1
    assert await state(db_session, delivery) == (
        DeliveryStatus.FAILED,
        1,
        "expired before sending",
    )
    # A 503 before expiry counts against the endpoint; a 429 doesn't.
    assert await subscription_state(db_session, delivery.subscription_id) == (True, counted)


async def test_redirect_is_not_followed(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(IP_URL).respond(307, headers={"Location": "http://169.254.169.254/"})
    elsewhere = respx_mock.route(host="169.254.169.254").respond(200)
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, notifier(webhook_http), delivery)

    assert outcome is DeliveryOutcome.FAILED
    assert not elsewhere.called
    assert "redirect not followed" in (await state(db_session, delivery))[2]


async def test_oversized_response_is_truncated(
    db_session: AsyncSession, webhook_http: httpx.AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(IP_URL).respond(200, content=b"x" * 1_000_000)
    subscription = await seed_webhook_subscription(
        db_session, url=URL, encrypted_secret=secret_box().encrypt(SECRET)
    )
    response = await notifier(webhook_http).send_test(recipient_of(subscription))

    assert response.status_code == 200
    assert response.truncated is True
    assert len(response.body) == 65536


# --- consecutive failures -------------------------------------------------------------------


async def test_subscription_is_deactivated_after_consecutive_failures_and_logged_once(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.notifications.dispatcher")
    route = respx_mock.post(IP_URL).respond(400)
    first = await new_delivery(db_session)
    subscription = await db_session.get(Subscription, first.subscription_id)
    assert subscription is not None
    webhook = notifier(webhook_http)

    for delivery in [first] + [
        await new_delivery(db_session, subscription=subscription) for _ in range(2)
    ]:
        assert await run(worker_ctx, webhook, delivery) is DeliveryOutcome.FAILED
    after_threshold = await new_delivery(db_session, subscription=subscription)
    outcome = await run(worker_ctx, webhook, after_threshold)

    assert await subscription_state(db_session, subscription.id) == (False, 3)
    assert outcome is DeliveryOutcome.INACTIVE
    assert route.call_count == 3
    messages = [r.getMessage() for r in caplog.records if "consecutive" in r.getMessage()]
    assert messages == ["webhook subscription deactivated after consecutive failed deliveries"]


async def test_a_success_resets_the_failure_count(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(IP_URL)
    route.side_effect = [httpx.Response(400), httpx.Response(400), httpx.Response(200)]
    first = await new_delivery(db_session)
    subscription = await db_session.get(Subscription, first.subscription_id)
    assert subscription is not None
    webhook = notifier(webhook_http)

    await run(worker_ctx, webhook, first)
    await run(worker_ctx, webhook, await new_delivery(db_session, subscription=subscription))
    assert await subscription_state(db_session, subscription.id) == (True, 2)
    await run(worker_ctx, webhook, await new_delivery(db_session, subscription=subscription))

    assert await subscription_state(db_session, subscription.id) == (True, 0)


async def test_expiry_without_any_attempt_is_not_the_receivers_fault(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
) -> None:
    delivery = await new_delivery(db_session, occurred_at=now() - timedelta(hours=1))

    assert await run(worker_ctx, notifier(webhook_http), delivery) is DeliveryOutcome.EXPIRED
    assert await subscription_state(db_session, delivery.subscription_id) == (True, 0)


# --- SSRF at send time ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        (["127.0.0.1"], "loopback"),
        (["169.254.169.254"], "cloud metadata"),
        (["::1"], "loopback"),
        (["10.0.0.7"], "private"),
        (["::ffff:127.0.0.1"], "embeds 127.0.0.1"),
    ],
)
async def test_name_resolving_to_an_internal_address_is_never_called(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    answers: list[str],
    reason: str,
) -> None:
    # e.g. DNS rebinding: the name resolved to a public address when the subscription was
    # created, and to an internal one now. The send-time check is the one that counts.
    anything = respx_mock.route().respond(200)
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, notifier(webhook_http, resolver({HOST: answers})), delivery)

    assert outcome is DeliveryOutcome.FAILED
    assert not anything.called
    error = (await state(db_session, delivery))[2]
    assert "unsafe webhook target" in error
    assert reason in error


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://169.254.169.254/latest/meta-data/",
        "http://hooks.example.com/hook",  # http outside development
    ],
)
async def test_unsafe_urls_are_never_called(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
    url: str,
) -> None:
    anything = respx_mock.route().respond(200)
    delivery = await new_delivery(db_session, url=url)

    assert await run(worker_ctx, notifier(webhook_http), delivery) is DeliveryOutcome.FAILED
    assert not anything.called


async def test_http_is_allowed_in_development_but_ssrf_rules_still_apply(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"http://{PUBLIC_IP}:80/hook").respond(200)
    public = await new_delivery(db_session, url=f"http://{HOST}/hook")
    local = await new_delivery(db_session, url="http://localhost:8080/hook")
    dev = notifier(webhook_http, resolver({"localhost": ["127.0.0.1"]}), allow_http=True)

    assert await run(worker_ctx, dev, public) is DeliveryOutcome.SENT
    assert await run(worker_ctx, dev, local) is DeliveryOutcome.FAILED
    assert route.call_count == 1


async def test_dns_failure_is_retried(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
) -> None:
    delivery = await new_delivery(db_session, url="https://gone.example/hook")

    with pytest.raises(RetryDeliveryError):
        await run(worker_ctx, notifier(webhook_http), delivery)


async def test_undecryptable_secret_fails_without_calling(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    webhook_http: httpx.AsyncClient,
    respx_mock: respx.MockRouter,
) -> None:
    anything = respx_mock.route().respond(200)
    subscription = await seed_webhook_subscription(
        db_session,
        url=URL,
        encrypted_secret="not-a-fernet-token",  # noqa: S106
    )
    delivery = await new_delivery(db_session, subscription=subscription)

    assert await run(worker_ctx, notifier(webhook_http), delivery) is DeliveryOutcome.FAILED
    assert not anything.called
    assert "cannot be decrypted" in (await state(db_session, delivery))[2]
