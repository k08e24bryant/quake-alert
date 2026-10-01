"""Ingestion -> match job -> delivery job, through the real worker jobs and the arq queue
(jobs are taken off the queue and run in-process). BMKG and Telegram are mocked."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from arq import ArqRedis, Retry
from arq.constants import default_queue_name, job_key_prefix
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryStatus, NotificationDelivery
from app.ingestion.domain import Feed
from tests.bmkg_samples import BASE_URL, load, make_item
from tests.integration.seed import JAKARTA, seed_quake, seed_subscription
from tests.telegram_samples import error, method_url, ok, sent_messages
from worker.jobs import deliver_notification, match_earthquakes, poll_bmkg_feeds

JOBS: dict[str, Callable[..., Awaitable[Any]]] = {
    "match_earthquakes": match_earthquakes,
    "deliver_notification": deliver_notification,
}


async def queued(redis: ArqRedis) -> list[str]:
    return sorted(job.function for job in await redis.queued_jobs())


async def drain(ctx: dict[str, Any]) -> list[str]:
    """Run queued jobs (and the jobs they enqueue) until the queue is empty."""
    redis: ArqRedis = ctx["redis"]
    ran = []
    while jobs := await redis.queued_jobs():
        for job in jobs:
            assert job.job_id is not None
            await redis.zrem(default_queue_name, job.job_id)
            await redis.delete(job_key_prefix + job.job_id)
            await JOBS[job.function](ctx, *job.args, **job.kwargs)
            ran.append(job.function)
    return ran


def fresh_autogempa(magnitude: str, occurred_at: datetime) -> dict[str, Any]:
    lat, lon = JAKARTA
    item = make_item(
        occurred_at=occurred_at,
        magnitude=magnitude,
        latitude=str(round(lat - 0.3, 2)),  # ~33 km south of Jakarta
        longitude=str(round(lon, 2)),
        region="Pusat gempa berada di darat 33 km Selatan Jakarta",
        potential="Gempa ini dirasakan untuk diteruskan pada masyarakat",
    )
    return {"Infogempa": {"gempa": item}}


def serve_bmkg(respx_mock: respx.MockRouter, autogempa: dict[str, Any]) -> None:
    respx_mock.get(f"{BASE_URL}autogempa.json").respond(json=autogempa)
    # The real samples: dozens of quakes, all older than NOTIFY_MAX_AGE_MINUTES.
    for feed in (Feed.GEMPATERKINI, Feed.GEMPADIRASAKAN):
        respx_mock.get(f"{BASE_URL}{feed.value}.json").respond(json=load(feed))


async def deliveries(session: AsyncSession) -> list[tuple[Any, ...]]:
    rows = await session.execute(
        select(NotificationDelivery.subscription_id, NotificationDelivery.status)
    )
    return [tuple(row) for row in rows]


@pytest.fixture
def send(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.post(method_url("sendMessage")).mock(return_value=ok())


async def test_fresh_quake_reaches_the_subscriber_once(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    send: respx.Route,
) -> None:
    subscription = await seed_subscription(db_session, at=JAKARTA, chat_id=1001)
    occurred_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=3)
    serve_bmkg(respx_mock, fresh_autogempa("4.6", occurred_at))

    await poll_bmkg_feeds(worker_ctx)
    # One match job per feed commit that changed rows.
    assert await queued(worker_ctx["redis"]) == ["match_earthquakes"] * 3
    ran = await drain(worker_ctx)

    assert ran.count("deliver_notification") == 1
    [message] = sent_messages(send)
    assert message["chat_id"] == 1001
    assert "Magnitudo: 4.6" in message["text"]
    assert await deliveries(db_session) == [(subscription.id, DeliveryStatus.SENT)]

    # The same content again: every feed skipped, nothing enqueued, nothing sent.
    await poll_bmkg_feeds(worker_ctx)
    assert await queued(worker_ctx["redis"]) == []
    # And re-matching the same rows (e.g. a duplicate match job) is a no-op too.
    earthquake_ids = [
        str(i) for i in (await db_session.scalars(select(NotificationDelivery.earthquake_id))).all()
    ]
    assert await match_earthquakes(worker_ctx, earthquake_ids) == 0
    assert await drain(worker_ctx) == []
    assert send.call_count == 1


async def test_first_run_backfill_of_old_quakes_sends_nothing(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    send: respx.Route,
) -> None:
    # A subscriber covering all of Indonesia, and only the (old) real samples.
    await seed_subscription(db_session, at=(-2.5, 118.0), radius_km=1000, min_magnitude="2.0")
    for feed in Feed:
        respx_mock.get(f"{BASE_URL}{feed.value}.json").respond(json=load(feed))

    await poll_bmkg_feeds(worker_ctx)
    await drain(worker_ctx)

    assert await db_session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    assert not send.called


async def test_revision_across_a_threshold_notifies_only_the_newly_matching_subscriber(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    send: respx.Route,
) -> None:
    low = await seed_subscription(db_session, at=JAKARTA, min_magnitude="4.0", chat_id=1)
    high = await seed_subscription(db_session, at=JAKARTA, min_magnitude="5.0", chat_id=2)
    occurred_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=3)

    serve_bmkg(respx_mock, fresh_autogempa("4.8", occurred_at))
    await poll_bmkg_feeds(worker_ctx)
    await drain(worker_ctx)
    first_round = sent_messages(send)
    assert [m["chat_id"] for m in first_round] == [1]

    # BMKG revises the same quake (same DateTime) to M5.2.
    respx_mock.reset()  # also forgets the calls recorded so far
    serve_bmkg(respx_mock, fresh_autogempa("5.2", occurred_at))
    await poll_bmkg_feeds(worker_ctx)
    await drain(worker_ctx)

    second_round = sent_messages(send)
    assert [m["chat_id"] for m in second_round] == [2]  # `low` is not alerted again
    assert "Magnitudo: 5.2" in second_round[0]["text"]
    assert sorted(await deliveries(db_session)) == sorted(
        [(low.id, DeliveryStatus.SENT), (high.id, DeliveryStatus.SENT)]
    )


async def test_update_that_changes_only_raw_enqueues_no_matching(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    send: respx.Route,
) -> None:
    occurred_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=3)
    payload = fresh_autogempa("4.8", occurred_at)
    serve_bmkg(respx_mock, payload)
    await poll_bmkg_feeds(worker_ctx)
    await drain(worker_ctx)

    # New content hash, but only a field we don't derive anything from (local time) changed.
    payload["Infogempa"]["gempa"]["Jam"] = "13:24:52 WIB"
    respx_mock.reset()
    serve_bmkg(respx_mock, payload)
    results = await poll_bmkg_feeds(worker_ctx)

    assert results["autogempa"] == "success"
    assert await queued(worker_ctx["redis"]) == []


async def test_delivery_job_turns_429_into_an_arq_retry_at_retry_after(
    worker_ctx: dict[str, Any], db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(method_url("sendMessage")).mock(
        return_value=error(429, "Too Many Requests: retry after 9", retry_after=9)
    )
    subscription = await seed_subscription(db_session)
    quake = await seed_quake(db_session, occurred_at=datetime.now(UTC), magnitude="5.0")
    delivery = NotificationDelivery(subscription_id=subscription.id, earthquake_id=quake.id)
    db_session.add(delivery)
    await db_session.flush()

    with pytest.raises(Retry) as caught:
        await deliver_notification(worker_ctx, str(delivery.id))

    assert caught.value.defer_score == 9000


async def test_delivery_job_retries_unexpected_errors_until_the_last_try(
    worker_ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr("worker.jobs.deliver", broken)

    with pytest.raises(Retry) as caught:
        await deliver_notification(
            {**worker_ctx, "job_try": 1}, "00000000-0000-0000-0000-000000000000"
        )
    assert caught.value.defer_score == 2000  # notify_retry_backoff_seconds * 2**0

    last_try = {**worker_ctx, "job_try": worker_ctx["notify_config"].max_attempts}
    with pytest.raises(RuntimeError):
        await deliver_notification(last_try, "00000000-0000-0000-0000-000000000000")


async def test_poll_requeues_pending_deliveries_whose_job_was_lost(
    worker_ctx: dict[str, Any],
    db_session: AsyncSession,
    respx_mock: respx.MockRouter,
    send: respx.Route,
) -> None:
    # A delivery committed, but the worker died before enqueueing its job.
    subscription = await seed_subscription(db_session, chat_id=77)
    quake = await seed_quake(db_session, occurred_at=datetime.now(UTC), magnitude="5.0")
    db_session.add(NotificationDelivery(subscription_id=subscription.id, earthquake_id=quake.id))
    await db_session.flush()
    for feed in Feed:
        respx_mock.get(f"{BASE_URL}{feed.value}.json").respond(503)

    await poll_bmkg_feeds(worker_ctx)
    await poll_bmkg_feeds(worker_ctx)  # enqueueing again while queued is a no-op (job id)

    assert await queued(worker_ctx["redis"]) == ["deliver_notification"]
    await drain(worker_ctx)
    assert [m["chat_id"] for m in sent_messages(send)] == [77]
