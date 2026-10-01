import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Earthquake, NotificationDelivery
from app.ingestion.dedup import DedupConfig, upsert_report
from app.ingestion.domain import Feed, QuakeReport
from app.notifications.matcher import MatchResult, match_flagged, pending_delivery_ids
from tests.bmkg_samples import BASE_URL, make_report
from tests.integration.seed import BANDUNG, BOGOR, JAKARTA, seed_quake, seed_subscription

MAX_AGE = timedelta(minutes=30)
DEDUP = DedupConfig(
    max_time_diff=timedelta(seconds=60), max_distance_m=50_000, shakemap_base_url=BASE_URL
)


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def match(session: AsyncSession) -> MatchResult:
    return await match_flagged(session, now=now(), max_age=MAX_AGE)


async def flagged(session: AsyncSession) -> set[uuid.UUID]:
    rows = await session.scalars(select(Earthquake.id).where(Earthquake.needs_matching))
    return set(rows.all())


async def delivered_pairs(session: AsyncSession) -> set[tuple[uuid.UUID, uuid.UUID]]:
    rows = await session.execute(
        select(NotificationDelivery.subscription_id, NotificationDelivery.earthquake_id)
    )
    return {(sub, quake) for sub, quake in rows}


# --- the matching rule --------------------------------------------------------------------


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
    quake = await seed_quake(
        db_session, at=quake_at, occurred_at=now(), magnitude="5.0", needs_matching=True
    )

    result = await match(db_session)

    assert bool(result.delivery_ids) is expected
    expected_pairs = {(subscription.id, quake.id)} if expected else set()
    assert await delivered_pairs(db_session) == expected_pairs


@pytest.mark.parametrize(("magnitude", "expected"), [("4.4", False), ("4.5", True), ("6.1", True)])
async def test_magnitude_threshold_is_inclusive(
    db_session: AsyncSession, magnitude: str, expected: bool
) -> None:
    await seed_subscription(db_session, min_magnitude="4.5")
    await seed_quake(db_session, occurred_at=now(), magnitude=magnitude, needs_matching=True)

    assert bool((await match(db_session)).delivery_ids) is expected


async def test_inactive_subscriptions_are_not_matched(db_session: AsyncSession) -> None:
    active = await seed_subscription(db_session)
    await seed_subscription(db_session, is_active=False)
    quake = await seed_quake(db_session, occurred_at=now(), magnitude="5.0", needs_matching=True)

    await match(db_session)

    assert await delivered_pairs(db_session) == {(active.id, quake.id)}


# --- the outbox -----------------------------------------------------------------------------


async def test_matching_clears_the_flag_and_is_idempotent(db_session: AsyncSession) -> None:
    subscription = await seed_subscription(db_session)
    quake = await seed_quake(db_session, occurred_at=now(), magnitude="5.0", needs_matching=True)

    first = await match(db_session)
    assert await flagged(db_session) == set()
    second = await match(db_session)  # nothing flagged any more

    assert len(first.delivery_ids) == 1
    assert first.matched == [quake.id]
    assert second == MatchResult()
    # Even re-flagging (a later change) can't create a second delivery for the same pair.
    quake.needs_matching = True
    await db_session.flush()
    assert (await match(db_session)).delivery_ids == []
    assert await delivered_pairs(db_session) == {(subscription.id, quake.id)}


async def test_only_flagged_rows_are_matched(db_session: AsyncSession) -> None:
    await seed_subscription(db_session)
    flagged_row = await seed_quake(
        db_session, occurred_at=now(), magnitude="5.0", needs_matching=True
    )
    await seed_quake(db_session, occurred_at=now(), magnitude="5.0")

    await match(db_session)

    assert {quake for _, quake in await delivered_pairs(db_session)} == {flagged_row.id}


async def test_clearing_the_flag_does_not_touch_updated_at(db_session: AsyncSession) -> None:
    quake = await seed_quake(db_session, occurred_at=now(), needs_matching=True)
    await db_session.refresh(quake)
    before = quake.updated_at

    await match(db_session)
    await db_session.refresh(quake)

    assert quake.needs_matching is False
    assert quake.updated_at == before


@pytest.mark.parametrize(("age_minutes", "expected"), [(29, True), (31, False), (600, False)])
async def test_only_fresh_quakes_are_notified(
    db_session: AsyncSession, age_minutes: int, expected: bool
) -> None:
    await seed_subscription(db_session)
    await seed_quake(
        db_session,
        occurred_at=now() - timedelta(minutes=age_minutes),
        magnitude="5.0",
        needs_matching=True,
    )

    assert bool((await match(db_session)).delivery_ids) is expected


async def test_stale_flagged_rows_are_cleared_without_notifying_and_logged(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.notifications.matcher")
    await seed_subscription(db_session)
    stale = await seed_quake(
        db_session, occurred_at=now() - timedelta(hours=2), magnitude="5.0", needs_matching=True
    )

    result = await match(db_session)

    assert result.expired == [stale.id]
    assert result.delivery_ids == []
    assert await flagged(db_session) == set()
    assert await delivered_pairs(db_session) == set()
    [record] = [r for r in caplog.records if r.name == "app.notifications.matcher"]
    assert record.getMessage() == "cleared match flags of quakes too old to notify"
    assert record.__dict__["earthquake_ids"] == [str(stale.id)]


async def test_ingestion_flags_inserts_and_derived_changes_only(db_session: AsyncSession) -> None:
    at = now()
    inserted = await upsert_report(
        db_session, make_report(Feed.GEMPATERKINI, occurred_at=at), DEDUP
    )
    await db_session.flush()
    assert await flagged(db_session) == {inserted.earthquake_id}
    await match(db_session)

    await upsert_report(db_session, make_report(Feed.GEMPATERKINI, occurred_at=at), DEDUP)
    await db_session.flush()
    assert await flagged(db_session) == set()  # unchanged: not flagged again

    revised = make_report(Feed.GEMPATERKINI, occurred_at=at, magnitude="4.4")
    await upsert_report(db_session, revised, DEDUP)
    await db_session.flush()
    assert await flagged(db_session) == {inserted.earthquake_id}


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
    await match(db_session)
    assert await delivered_pairs(db_session) == {(low.id, inserted.earthquake_id)}

    # BMKG revises the same quake (same feed, same DateTime) above `high`'s threshold.
    revised = await upsert_report(db_session, report("5.1"), DEDUP)
    assert revised.earthquake_id == inserted.earthquake_id
    new = (await match(db_session)).delivery_ids

    assert len(new) == 1
    assert await delivered_pairs(db_session) == {
        (low.id, inserted.earthquake_id),
        (high.id, inserted.earthquake_id),
    }

    # Further revisions never alert anyone twice about the same row.
    await upsert_report(db_session, report("5.3"), DEDUP)
    assert (await match(db_session)).delivery_ids == []


async def test_downward_revision_keeps_existing_deliveries(db_session: AsyncSession) -> None:
    subscription = await seed_subscription(db_session, min_magnitude="5.0")
    quake = await seed_quake(db_session, occurred_at=now(), magnitude="5.2", needs_matching=True)
    await match(db_session)

    quake.magnitude = quake.magnitude - 1
    quake.needs_matching = True
    await db_session.flush()
    await match(db_session)

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
