import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DeliveryStatus, NotificationDelivery, Subscription
from app.notifications.dispatcher import (
    DeliveryOutcome,
    NotifyConfig,
    RetryDeliveryError,
    deliver,
)
from app.notifications.telegram import TelegramClient
from tests.integration.seed import BOGOR, JAKARTA, SURABAYA, seed_quake, seed_subscription
from tests.telegram_samples import error, method_url, ok, sent_messages

CHAT = 5150
CONFIG = NotifyConfig(
    max_age=timedelta(minutes=30),
    max_attempts=3,
    retry_backoff_seconds=2,
    duplicate_window=timedelta(seconds=120),
    duplicate_distance_m=100_000,
)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def send(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.post(method_url("sendMessage"))


async def new_delivery(
    session: AsyncSession,
    *,
    subscription: Subscription | None = None,
    quake_at: tuple[float, float] = BOGOR,
    occurred_at: datetime | None = None,
    **quake: Any,
) -> NotificationDelivery:
    subscription = subscription or await seed_subscription(session, at=JAKARTA, chat_id=CHAT)
    row = await seed_quake(
        session, at=quake_at, occurred_at=occurred_at or now() - timedelta(minutes=2), **quake
    )
    delivery = NotificationDelivery(subscription_id=subscription.id, earthquake_id=row.id)
    session.add(delivery)
    await session.flush()
    return delivery


async def run(
    worker_ctx: dict[str, Any], delivery: NotificationDelivery, attempt: int = 1
) -> DeliveryOutcome:
    telegram: TelegramClient = worker_ctx["telegram"]
    factory: async_sessionmaker[AsyncSession] = worker_ctx["session_factory"]
    return await deliver(
        factory, telegram, delivery.id, attempt=attempt, now=datetime.now(UTC), config=CONFIG
    )


async def state(session: AsyncSession, delivery_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            select(
                NotificationDelivery.status,
                NotificationDelivery.attempts,
                NotificationDelivery.last_error,
                NotificationDelivery.sent_at,
            ).where(NotificationDelivery.id == delivery_id)
        )
    ).one()
    return dict(row._mapping)


async def is_active(session: AsyncSession, subscription_id: uuid.UUID) -> bool | None:
    return await session.scalar(
        select(Subscription.is_active).where(Subscription.id == subscription_id)
    )


async def test_sends_the_alert_and_records_it(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=ok({"message_id": 1}))
    delivery = await new_delivery(
        db_session, magnitude="5.4", region="Bogor test", potential="Tidak berpotensi tsunami"
    )

    outcome = await run(worker_ctx, delivery)

    assert outcome is DeliveryOutcome.SENT
    [message] = sent_messages(send)
    assert message["chat_id"] == CHAT
    text = message["text"]
    for expected in (
        "Magnitudo: 5.4",
        "Wilayah: Bogor test",
        "Kedalaman: 10 km",
        "Jarak dari lokasi Anda: sekitar 43 km",  # Jakarta -> Bogor, by PostGIS
        "Potensi (BMKG): Tidak berpotensi tsunami",
        "WIB",
        "Sumber: BMKG (https://www.bmkg.go.id)",
    ):
        assert expected in text
    assert not text.startswith("Catatan")
    recorded = await state(db_session, delivery.id)
    assert (recorded["status"], recorded["attempts"]) == (DeliveryStatus.SENT, 1)
    assert recorded["sent_at"] is not None


async def test_sent_delivery_is_not_sent_again(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=ok())
    delivery = await new_delivery(db_session)

    assert await run(worker_ctx, delivery) is DeliveryOutcome.SENT
    assert await run(worker_ctx, delivery) is DeliveryOutcome.NOT_PENDING
    assert send.call_count == 1


async def test_429_retries_after_telegrams_retry_after(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=error(429, "Too Many Requests: retry after 17", retry_after=17))
    delivery = await new_delivery(db_session)

    with pytest.raises(RetryDeliveryError) as caught:
        await run(worker_ctx, delivery, attempt=1)

    assert caught.value.defer_seconds == 17  # not the exponential backoff
    recorded = await state(db_session, delivery.id)
    assert (recorded["status"], recorded["attempts"]) == (DeliveryStatus.PENDING, 1)
    assert "429" in recorded["last_error"]


async def test_429_then_success(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.side_effect = [error(429, "Too Many Requests", retry_after=3), ok()]
    delivery = await new_delivery(db_session)

    with pytest.raises(RetryDeliveryError):
        await run(worker_ctx, delivery, attempt=1)
    assert await run(worker_ctx, delivery, attempt=2) is DeliveryOutcome.SENT

    recorded = await state(db_session, delivery.id)
    assert (recorded["status"], recorded["attempts"], recorded["last_error"]) == (
        DeliveryStatus.SENT,
        2,
        None,
    )


async def test_429_on_the_last_attempt_gives_up(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=error(429, "Too Many Requests", retry_after=5))
    delivery = await new_delivery(db_session)

    outcome = await run(worker_ctx, delivery, attempt=CONFIG.max_attempts)

    assert outcome is DeliveryOutcome.FAILED
    assert (await state(db_session, delivery.id))["status"] is DeliveryStatus.FAILED


async def test_403_deactivates_the_subscription_without_retrying(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=error(403, "Forbidden: bot was blocked by the user"))
    subscription = await seed_subscription(db_session, at=JAKARTA, chat_id=CHAT)
    delivery = await new_delivery(db_session, subscription=subscription)
    later = await new_delivery(db_session, subscription=subscription)

    outcome = await run(worker_ctx, delivery)  # no RetryDeliveryError

    assert outcome is DeliveryOutcome.DEACTIVATED
    assert await is_active(db_session, subscription.id) is False
    recorded = await state(db_session, delivery.id)
    assert (recorded["status"], recorded["attempts"]) == (DeliveryStatus.FAILED, 1)
    assert "blocked" in recorded["last_error"]
    # Another alert already queued for the same subscriber isn't attempted.
    assert await run(worker_ctx, later) is DeliveryOutcome.INACTIVE
    assert send.call_count == 1


@pytest.mark.parametrize(("attempt", "expected_defer"), [(1, 2.0), (2, 4.0)])
async def test_server_errors_back_off_exponentially(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    send: respx.Route,
    attempt: int,
    expected_defer: float,
) -> None:
    send.mock(return_value=error(502, "Bad Gateway"))
    delivery = await new_delivery(db_session)

    with pytest.raises(RetryDeliveryError) as caught:
        await run(worker_ctx, delivery, attempt=attempt)

    assert caught.value.defer_seconds == expected_defer


async def test_server_error_on_the_last_attempt_gives_up(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=error(500, "Internal Server Error"))
    delivery = await new_delivery(db_session)

    assert await run(worker_ctx, delivery, attempt=3) is DeliveryOutcome.FAILED
    assert (await state(db_session, delivery.id))["status"] is DeliveryStatus.FAILED


async def test_other_client_errors_fail_without_retry(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=error(400, "Bad Request: chat not found"))
    delivery = await new_delivery(db_session)

    assert await run(worker_ctx, delivery) is DeliveryOutcome.FAILED
    recorded = await state(db_session, delivery.id)
    assert recorded["status"] is DeliveryStatus.FAILED
    assert "chat not found" in recorded["last_error"]


async def test_quake_too_old_by_send_time_is_not_sent(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    delivery = await new_delivery(db_session, occurred_at=now() - timedelta(minutes=31))

    assert await run(worker_ctx, delivery) is DeliveryOutcome.EXPIRED
    assert not send.called
    recorded = await state(db_session, delivery.id)
    assert (recorded["status"], recorded["attempts"]) == (DeliveryStatus.FAILED, 0)


async def test_delivery_deleted_by_stop_is_not_sent(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    delivery = await new_delivery(db_session)
    await db_session.execute(delete(Subscription).where(Subscription.telegram_chat_id == CHAT))

    assert await run(worker_ctx, delivery) is DeliveryOutcome.NOT_PENDING
    assert not send.called


# --- duplicate rows (kept by dedup on purpose) ------------------------------------------------


async def test_alert_for_a_likely_duplicate_row_is_sent_with_a_prefix(
    worker_ctx: dict[str, Any], db_session: AsyncSession, send: respx.Route
) -> None:
    send.mock(return_value=ok())
    subscription = await seed_subscription(db_session, at=JAKARTA, chat_id=CHAT)
    first_at = now() - timedelta(minutes=3)
    first = await new_delivery(db_session, subscription=subscription, occurred_at=first_at)
    await run(worker_ctx, first)
    # The same event as another row: 90 s later and ~10 km away.
    duplicate = await new_delivery(
        db_session,
        subscription=subscription,
        occurred_at=first_at + timedelta(seconds=90),
        quake_at=(BOGOR[0] - 0.09, BOGOR[1]),
    )

    assert await run(worker_ctx, duplicate) is DeliveryOutcome.SENT

    first_text, duplicate_text = (m["text"] for m in sent_messages(send))
    prefix, rest = duplicate_text.split("\n", 1)
    assert prefix.startswith("Catatan: mungkin kejadian yang sama dengan info gempa sebelumnya")
    assert "feed BMKG lain" in prefix
    assert rest.startswith("Info gempa")
    assert not first_text.startswith("Catatan")


@pytest.mark.parametrize(
    ("seconds_later", "second_at", "first_status"),
    [
        (150, BOGOR, DeliveryStatus.SENT),  # too far apart in time
        (30, SURABAYA, DeliveryStatus.SENT),  # too far apart in space (~620 km)
        (30, BOGOR, DeliveryStatus.FAILED),  # the subscriber never received the first one
    ],
)
async def test_no_prefix_when_not_a_likely_duplicate(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    send: respx.Route,
    seconds_later: int,
    second_at: tuple[float, float],
    first_status: DeliveryStatus,
) -> None:
    send.mock(return_value=ok())
    subscription = await seed_subscription(db_session, at=JAKARTA, radius_km=1000, chat_id=CHAT)
    first_at = now() - timedelta(minutes=3)
    first = await new_delivery(db_session, subscription=subscription, occurred_at=first_at)
    first.status = first_status
    await db_session.flush()
    second = await new_delivery(
        db_session,
        subscription=subscription,
        occurred_at=first_at + timedelta(seconds=seconds_later),
        quake_at=second_at,
    )

    await run(worker_ctx, second)

    [message] = sent_messages(send)
    assert message["text"].startswith("Info gempa")
