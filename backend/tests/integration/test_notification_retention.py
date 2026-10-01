from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryStatus, NotificationDelivery
from tests.integration.seed import seed_quake, seed_subscription
from worker.jobs import prune_old_records


async def test_daily_prune_deletes_old_sent_and_failed_but_never_pending(
    worker_ctx: dict[str, Any], db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    subscription = await seed_subscription(db_session)
    rows = {
        # (status, age in days) -> kept?
        (DeliveryStatus.SENT, 31): False,
        (DeliveryStatus.FAILED, 31): False,
        (DeliveryStatus.PENDING, 400): True,  # however old
        (DeliveryStatus.SENT, 29): True,
        (DeliveryStatus.FAILED, 1): True,
    }
    ids = {}
    for status, days in rows:
        quake = await seed_quake(db_session)
        delivery = NotificationDelivery(
            subscription_id=subscription.id,
            earthquake_id=quake.id,
            status=status,
            created_at=now - timedelta(days=days),
        )
        db_session.add(delivery)
        await db_session.flush()
        ids[(status, days)] = delivery.id

    deleted = await prune_old_records(worker_ctx)

    assert deleted["notification_deliveries"] == 2
    remaining = set((await db_session.scalars(select(NotificationDelivery.id))).all())
    assert remaining == {ids[key] for key, kept in rows.items() if kept}
