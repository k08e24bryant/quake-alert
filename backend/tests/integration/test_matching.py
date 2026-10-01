import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationDelivery
from app.ingestion.dedup import DedupConfig, upsert_report
from app.ingestion.domain import Feed, QuakeReport
from app.notifications.matcher import create_deliveries, pending_delivery_ids
from tests.bmkg_samples import BASE_URL, make_report
from tests.integration.seed import BANDUNG, BOGOR, JAKARTA, seed_quake, seed_subscription

MAX_AGE = timedelta(minutes=30)
DEDUP = DedupConfig(
    max_time_diff=timedelta(seconds=60), max_distance_m=50_000, shakemap_base_url=BASE_URL
)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def match(session: AsyncSession, *earthquake_ids: uuid.UUID) -> list[uuid.UUID]:
    return await create_deliveries(session, earthquake_ids, now=now(), max_age=MAX_AGE)


async def delivered_pairs(session: AsyncSession) -> set[tuple[uuid.UUID, uuid.UUID]]:
    rows = await session.execute(
        select(NotificationDelivery.subscription_id, NotificationDelivery.earthquake_id)
    )
    return {(sub, quake) for sub, quake in rows}


@pytest.mark.parametrize(
    ("radius_km", "quake_at", "expected"),
    [
        (50, BOGOR, True),  # ~43 km
        (40, BOGOR, False),
        (150, BANDUNG, True),  # ~116 km
        (100, BANDUNG, False),
    ],
)
async def test_radius(
    db_session: AsyncSession,
    radius_km: int,
    quake_at: tuple[float, float],
    expected: bool,
) -> None:
    subscription = await seed_subscription(db_session, at=JAKARTA, radius_km=radius_km)
    quake = await seed_quake(db_session, at=quake_at, occurred_at=now(), magnitude="5.0")

    created = await match(db_session, quake.id)

    assert bool(created) is expected
    assert await delivered_pairs(db_session) == (
        {(subscription.id, quake.id)} if expected else set()
    )


@pytest.mark.parametrize(("magnitude", "expected"), [("4.4", False), ("4.5", True), ("6.1", True)])
async def test_magnitude_threshold_is_inclusive(
    db_session: AsyncSession, magnitude: str, expected: bool
) -> None:
    await seed_subscription(db_session, min_magnitude="4.5")
    quake = await seed_quake(db_session, occurred_at=now(), magnitude=magnitude)

    assert bool(await match(db_session, quake.id)) is expected


async def test_inactive_subscriptions_are_not_matched(db_session: AsyncSession) -> None:
    active = await seed_subscription(db_session)
    await seed_subscription(db_session, is_active=False)
    quake = await seed_quake(db_session, occurred_at=now(), magnitude="5.0")

    await match(db_session, quake.id)

    assert await delivered_pairs(db_session) == {(active.id, quake.id)}


@pytest.mark.parametrize(("age_minutes", "expected"), [(29, True), (31, False), (600, False)])
async def test_only_fresh_quakes_are_matched(
    db_session: AsyncSession, age_minutes: int, expected: bool
) -> None:
    await seed_subscription(db_session)
    quake = await seed_quake(
        db_session, occurred_at=now() - timedelta(minutes=age_minutes), magnitude="5.0"
    )

    assert bool(await match(db_session, quake.id)) is expected


async def test_matching_is_idempotent(db_session: AsyncSession) -> None:
    subscription = await seed_subscription(db_session)
    quake = await seed_quake(db_session, occurred_at=now(), magnitude="5.0")

    first = await match(db_session, quake.id)
    second = await match(db_session, quake.id)

    assert len(first) == 1
    assert second == []
    assert await delivered_pairs(db_session) == {(subscription.id, quake.id)}


async def test_only_the_given_rows_are_matched(db_session: AsyncSession) -> None:
    await seed_subscription(db_session)
    given = await seed_quake(db_session, occurred_at=now(), magnitude="5.0")
    await seed_quake(db_session, occurred_at=now(), magnitude="5.0")

    await match(db_session, given.id)

    assert {quake for _, quake in await delivered_pairs(db_session)} == {given.id}


async def test_threshold_crossing_revision_notifies_newly_matching_subscribers_once(
    db_session: AsyncSession,
) -> None:
    at = now()
    low = await seed_subscription(db_session, min_magnitude="4.0")
    high = await seed_subscription(db_session, min_magnitude="5.0")
    lat, lon = (str(c) for c in JAKARTA)

    def report(magnitude: str) -> QuakeReport:
        return make_report(
            Feed.AUTOGEMPA, occurred_at=at, magnitude=magnitude, latitude=lat, longitude=lon
        )

    inserted = await upsert_report(db_session, report("4.5"), DEDUP)
    await match(db_session, inserted.earthquake_id)
    assert await delivered_pairs(db_session) == {(low.id, inserted.earthquake_id)}

    # BMKG revises the same quake (same feed, same DateTime) above `high`'s threshold.
    revised = await upsert_report(db_session, report("5.1"), DEDUP)
    assert revised.earthquake_id == inserted.earthquake_id
    assert revised.fields_changed
    new = await match(db_session, revised.earthquake_id)

    assert len(new) == 1
    assert await delivered_pairs(db_session) == {
        (low.id, inserted.earthquake_id),
        (high.id, inserted.earthquake_id),
    }

    # Further revisions never alert anyone twice about the same row.
    await upsert_report(db_session, report("5.3"), DEDUP)
    assert await match(db_session, inserted.earthquake_id) == []


async def test_downward_revision_keeps_existing_deliveries(db_session: AsyncSession) -> None:
    subscription = await seed_subscription(db_session, min_magnitude="5.0")
    quake = await seed_quake(db_session, occurred_at=now(), magnitude="5.2")
    await match(db_session, quake.id)

    quake.magnitude = quake.magnitude - 1
    await db_session.flush()
    await match(db_session, quake.id)

    assert await delivered_pairs(db_session) == {(subscription.id, quake.id)}


async def test_pending_delivery_ids_lists_fresh_pending_only(db_session: AsyncSession) -> None:
    subscription = await seed_subscription(db_session)
    fresh = await seed_quake(db_session, occurred_at=now(), magnitude="5.0")
    old = await seed_quake(db_session, occurred_at=now() - timedelta(hours=2), magnitude="5.0")
    pending = NotificationDelivery(subscription_id=subscription.id, earthquake_id=fresh.id)
    db_session.add_all(
        [pending, NotificationDelivery(subscription_id=subscription.id, earthquake_id=old.id)]
    )
    await db_session.flush()

    assert await pending_delivery_ids(db_session, now=now(), max_age=MAX_AGE) == [pending.id]
