from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import DeliveryStatus, NotificationDelivery


async def prune_notification_deliveries(
    session: AsyncSession, now: datetime, settings: Settings
) -> int:
    """Delete sent and failed deliveries created more than
    notification_deliveries_retention_days ago; return how many. Pending ones are never
    deleted, whatever their age: they are still owed to someone (and expire on their own
    once the quake is too old to send)."""
    cutoff = now - timedelta(days=settings.notification_deliveries_retention_days)
    deleted = await session.scalars(
        delete(NotificationDelivery)
        .where(
            NotificationDelivery.status.in_([DeliveryStatus.SENT, DeliveryStatus.FAILED]),
            NotificationDelivery.created_at < cutoff,
        )
        .returning(NotificationDelivery.id)
    )
    return len(deleted.all())
