"""Who should be alerted about which earthquake rows.

earthquakes.needs_matching is a transactional outbox: ingestion sets it in the same
transaction as an insert or a derived-field change, and match_flagged() clears it in the
same transaction that inserts the deliveries. Whatever happens to the job queue in between,
a flagged row is matched exactly once per change (a lost enqueue is picked up by the next
poll's sweep).

Matching runs in SQL on the rows' current values in the database (never a cache), and is
idempotent: a (subscription, earthquake) pair gets at most one delivery, however often the
row is re-matched. That is also how a revision is handled: re-matching a row whose
magnitude rose across a subscriber's threshold creates that subscriber's delivery; everyone
already matched keeps the one they had.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryStatus, Earthquake, NotificationDelivery, Subscription

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MatchResult:
    delivery_ids: list[uuid.UUID] = field(default_factory=list)  # new deliveries to send
    matched: list[uuid.UUID] = field(default_factory=list)  # fresh rows that were matched
    expired: list[uuid.UUID] = field(default_factory=list)  # too old: cleared, not notified


async def match_flagged(session: AsyncSession, *, now: datetime, max_age: timedelta) -> MatchResult:
    """Drain the outbox. Call inside a transaction; commit it to make the deliveries and the
    cleared flags durable together.

    Flagged rows are locked with SKIP LOCKED, so concurrent match jobs (or a sweep running
    alongside one) split the work instead of waiting, and a row that ingestion is changing
    right now is left for the run after that commit (which flags it again anyway).
    """
    flagged = (
        await session.execute(
            select(Earthquake.id, Earthquake.occurred_at)
            .where(Earthquake.needs_matching)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not flagged:
        return MatchResult()

    cutoff = now - max_age
    fresh = [row_id for row_id, occurred_at in flagged if occurred_at >= cutoff]
    expired = [row_id for row_id, occurred_at in flagged if occurred_at < cutoff]
    delivery_ids = await _create_deliveries(session, fresh) if fresh else []
    await session.execute(
        update(Earthquake)
        .where(Earthquake.id.in_([row_id for row_id, _ in flagged]))
        # Keep updated_at: clearing the outbox flag is not a change to the quake.
        .values(needs_matching=False, updated_at=Earthquake.updated_at)
    )
    if expired:
        logger.info(
            "cleared match flags of quakes too old to notify",
            extra={"count": len(expired), "earthquake_ids": [str(i) for i in expired]},
        )
    return MatchResult(delivery_ids=delivery_ids, matched=fresh, expired=expired)


async def _create_deliveries(
    session: AsyncSession, earthquake_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Insert a pending delivery for every active subscription each row matches, and return
    the ids of the deliveries that are new."""
    matches = (
        select(Subscription.id, Earthquake.id)
        .select_from(Earthquake)
        .join(
            Subscription,
            Subscription.is_active
            & (Earthquake.magnitude >= Subscription.min_magnitude)
            # geography + meters, so the GIST index on subscriptions.location is used.
            & func.ST_DWithin(
                Subscription.location, Earthquake.location, Subscription.radius_km * 1000
            ),
        )
        .where(Earthquake.id.in_(earthquake_ids))
    )
    inserted = await session.scalars(
        insert(NotificationDelivery)
        .from_select(["subscription_id", "earthquake_id"], matches)
        .on_conflict_do_nothing(
            index_elements=[
                NotificationDelivery.subscription_id,
                NotificationDelivery.earthquake_id,
            ]
        )
        .returning(NotificationDelivery.id)
    )
    return list(inserted.all())


async def pending_delivery_ids(
    session: AsyncSession, *, now: datetime, max_age: timedelta
) -> list[uuid.UUID]:
    """Pending deliveries of quakes still fresh enough to send: the safety net for a delivery
    whose job was never enqueued (e.g. the worker died between commit and enqueue)."""
    result = await session.scalars(
        select(NotificationDelivery.id)
        .join(Earthquake, Earthquake.id == NotificationDelivery.earthquake_id)
        .where(
            NotificationDelivery.status == DeliveryStatus.PENDING,
            Earthquake.occurred_at >= now - max_age,
        )
    )
    return list(result.all())
