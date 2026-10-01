"""Who should be alerted about which earthquake rows. Matching runs in SQL on the rows'
current values in the database (never a cache), and is idempotent: a (subscription,
earthquake) pair gets at most one delivery, however often the row is re-matched.

That is also how a revision is handled: re-matching a row whose magnitude rose across a
subscriber's threshold creates that subscriber's delivery; everyone already matched keeps
the one they had.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryStatus, Earthquake, NotificationDelivery, Subscription


async def create_deliveries(
    session: AsyncSession,
    earthquake_ids: Sequence[uuid.UUID],
    *,
    now: datetime,
    max_age: timedelta,
) -> list[uuid.UUID]:
    """Insert a pending delivery for every active subscription each fresh row matches, and
    return the ids of the deliveries that are new."""
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
        .where(Earthquake.id.in_(earthquake_ids), Earthquake.occurred_at >= now - max_age)
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
