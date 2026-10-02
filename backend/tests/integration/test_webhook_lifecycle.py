"""A webhook subscription's lifecycle in the database: pending verification is never
matched, can't be active, and is pruned after a day; verified ones are kept."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationDelivery, Subscription
from app.notifications.matcher import match_flagged
from tests.integration.seed import (
    BOGOR,
    JAKARTA,
    seed_quake,
    seed_subscription,
    seed_webhook_subscription,
)
from tests.webhook_samples import URL, secret_box
from worker.jobs import prune_old_records


async def webhook(session: AsyncSession, **kwargs: Any) -> Subscription:
    return await seed_webhook_subscription(
        session, url=URL, encrypted_secret=secret_box().encrypt("whsec_x"), at=JAKARTA, **kwargs
    )


async def test_pending_subscription_is_never_matched(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    pending = await webhook(db_session, verified=False)
    verified = await webhook(db_session)
    quake = await seed_quake(
        db_session, at=BOGOR, magnitude="6.0", occurred_at=now, needs_matching=True
    )

    result = await match_flagged(db_session, now=now, max_age=timedelta(minutes=30))

    matched = (
        await db_session.scalars(
            select(NotificationDelivery.subscription_id).where(
                NotificationDelivery.earthquake_id == quake.id
            )
        )
    ).all()
    assert matched == [verified.id]
    assert pending.id not in matched
    assert len(result.delivery_ids) == 1


async def test_the_database_refuses_an_active_unverified_webhook(
    db_session: AsyncSession,
) -> None:
    pending = await webhook(db_session, verified=False)

    with pytest.raises(IntegrityError, match="ck_subscriptions_webhook_verified"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Subscription).where(Subscription.id == pending.id).values(is_active=True)
            )


async def test_daily_prune_deletes_pending_subscriptions_older_than_a_day(
    worker_ctx: dict[str, Any], db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(hours=25)
    rows = {
        "pending, 25 h": (await webhook(db_session, verified=False, created_at=old), False),
        "pending, 23 h": (
            await webhook(db_session, verified=False, created_at=now - timedelta(hours=23)),
            True,
        ),
        "verified, 25 h": (await webhook(db_session, created_at=old), True),
        # Inactive is not pending: deactivated after it was verified. Kept (and never
        # reactivated); its owner deletes it.
        "inactive, 25 h": (await webhook(db_session, is_active=False, created_at=old), True),
        "telegram": (await seed_subscription(db_session), True),
    }

    deleted = await prune_old_records(worker_ctx)

    assert deleted["pending_webhook_subscriptions"] == 1
    remaining = set((await db_session.scalars(select(Subscription.id))).all())
    assert remaining == {row.id for row, kept in rows.values() if kept}
