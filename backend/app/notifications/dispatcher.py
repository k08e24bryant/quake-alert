"""Send one delivery (one alert to one subscriber) and record what happened.

Called by the worker's per-delivery arq job. Retrying is the job's business: this module
raises RetryDelivery with how long to wait, and gives up by itself on the final attempt, so
a delivery never stays `pending` after its last try.

Delivery is at-least-once: if Telegram accepted the message but recording `sent` fails,
the retry sends it again. Per the safety principle a duplicate beats a missed alert.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Float, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.db.models import DeliveryStatus, Earthquake, NotificationDelivery, Subscription
from app.notifications.messages import Alert, render_alert
from app.notifications.subscriptions import deactivate
from app.notifications.telegram import (
    TelegramClient,
    TelegramError,
    TelegramForbiddenError,
    TelegramRetryAfterError,
    TelegramUnavailableError,
)

logger = logging.getLogger(__name__)


class DeliveryOutcome(StrEnum):
    SENT = "sent"
    NOT_PENDING = "not_pending"  # deleted with its subscription (/stop), or already done
    EXPIRED = "expired"  # the quake is older than notify_max_age_minutes by now
    INACTIVE = "inactive"  # the subscription was deactivated after matching
    DEACTIVATED = "deactivated"  # Telegram 403: the user blocked the bot
    FAILED = "failed"  # permanent error, or retries exhausted


class RetryDeliveryError(Exception):
    def __init__(self, defer_seconds: float) -> None:
        super().__init__(f"retry in {defer_seconds}s")
        self.defer_seconds = defer_seconds


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    max_age: timedelta
    max_attempts: int
    retry_backoff_seconds: float
    duplicate_window: timedelta
    duplicate_distance_m: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "NotifyConfig":
        return cls(
            max_age=timedelta(minutes=settings.notify_max_age_minutes),
            max_attempts=settings.notify_max_attempts,
            retry_backoff_seconds=settings.notify_retry_backoff_seconds,
            duplicate_window=timedelta(seconds=settings.notify_duplicate_window_seconds),
            duplicate_distance_m=settings.notify_duplicate_distance_km * 1000,
        )

    def backoff_seconds(self, attempt: int) -> float:
        """Delay before the try after `attempt` (1-based)."""
        return self.retry_backoff_seconds * 2.0 ** (attempt - 1)


@dataclass(frozen=True, slots=True)
class _Pending:
    subscription_id: uuid.UUID
    chat_id: int | None
    is_active: bool
    alert: Alert


async def deliver(
    session_factory: async_sessionmaker[AsyncSession],
    telegram: TelegramClient,
    delivery_id: uuid.UUID,
    *,
    attempt: int,
    now: datetime,
    config: NotifyConfig,
) -> DeliveryOutcome:
    """Try to send the delivery once. `attempt` is 1-based. Raises RetryDeliveryError when
    a later attempt may succeed."""
    async with session_factory() as session:
        pending = await _load(session, delivery_id, config)
    if pending is None:
        return DeliveryOutcome.NOT_PENDING
    if not pending.is_active or pending.chat_id is None:
        await _record(session_factory, delivery_id, DeliveryStatus.FAILED, "subscription inactive")
        return DeliveryOutcome.INACTIVE
    if pending.alert.occurred_at < now - config.max_age:
        await _record(session_factory, delivery_id, DeliveryStatus.FAILED, "expired before sending")
        return DeliveryOutcome.EXPIRED

    try:
        await telegram.send_message(pending.chat_id, render_alert(pending.alert))
    except TelegramForbiddenError as exc:
        async with session_factory() as session, session.begin():
            await deactivate(session, pending.subscription_id)
            await _update(session, delivery_id, DeliveryStatus.FAILED, exc.description, tried=True)
        logger.info(
            "subscriber blocked the bot; subscription deactivated",
            extra={
                "delivery_id": str(delivery_id),
                "subscription_id": str(pending.subscription_id),
            },
        )
        return DeliveryOutcome.DEACTIVATED
    except (TelegramRetryAfterError, TelegramUnavailableError) as exc:
        if attempt >= config.max_attempts:
            return await _give_up(session_factory, delivery_id, exc, attempt)
        await _record(
            session_factory, delivery_id, DeliveryStatus.PENDING, exc.description, tried=True
        )
        if isinstance(exc, TelegramRetryAfterError):
            defer = float(exc.retry_after)
        else:
            defer = config.backoff_seconds(attempt)
        logger.warning(
            "alert delivery failed, will retry",
            extra={"delivery_id": str(delivery_id), "attempt": attempt, "retry_in_s": defer},
        )
        raise RetryDeliveryError(defer) from exc
    except TelegramError as exc:  # any other 4xx: retrying won't help
        return await _give_up(session_factory, delivery_id, exc, attempt)

    await _record(session_factory, delivery_id, DeliveryStatus.SENT, None, tried=True, sent_at=now)
    return DeliveryOutcome.SENT


async def _give_up(
    session_factory: async_sessionmaker[AsyncSession],
    delivery_id: uuid.UUID,
    exc: TelegramError,
    attempt: int,
) -> DeliveryOutcome:
    await _record(session_factory, delivery_id, DeliveryStatus.FAILED, exc.description, tried=True)
    logger.warning(
        "alert delivery failed permanently",
        extra={"delivery_id": str(delivery_id), "attempt": attempt, "error": exc.description},
    )
    return DeliveryOutcome.FAILED


async def _load(
    session: AsyncSession, delivery_id: uuid.UUID, config: NotifyConfig
) -> _Pending | None:
    distance_m = func.ST_Distance(Subscription.location, Earthquake.location, type_=Float)
    row = (
        await session.execute(
            select(
                NotificationDelivery.subscription_id,
                NotificationDelivery.earthquake_id,
                Subscription.telegram_chat_id,
                Subscription.is_active,
                Earthquake.magnitude,
                Earthquake.region,
                Earthquake.depth_km,
                Earthquake.occurred_at,
                Earthquake.potential,
                Earthquake.shakemap_url,
                Earthquake.is_synthetic,
                (distance_m / 1000).label("distance_km"),
            )
            .join(Subscription, Subscription.id == NotificationDelivery.subscription_id)
            .join(Earthquake, Earthquake.id == NotificationDelivery.earthquake_id)
            .where(
                NotificationDelivery.id == delivery_id,
                NotificationDelivery.status == DeliveryStatus.PENDING,
            )
        )
    ).first()
    if row is None:
        return None
    alert = Alert(
        magnitude=row.magnitude,
        region=row.region,
        depth_km=row.depth_km,
        occurred_at=row.occurred_at,
        distance_km=row.distance_km,
        potential=row.potential,
        shakemap_url=row.shakemap_url,
        possible_duplicate_of=await _already_alerted_nearby(
            session, row.subscription_id, row.earthquake_id, config
        ),
        is_test=row.is_synthetic,
    )
    return _Pending(row.subscription_id, row.telegram_chat_id, row.is_active, alert)


async def _already_alerted_nearby(
    session: AsyncSession,
    subscription_id: uuid.UUID,
    earthquake_id: uuid.UUID,
    config: NotifyConfig,
) -> datetime | None:
    """Origin time of another row this subscriber was already alerted about, close enough
    in time and space to be the same event (a duplicate row dedup chose to keep)."""
    previous = aliased(Earthquake)
    current = aliased(Earthquake)
    occurred_at: datetime | None = await session.scalar(
        select(previous.occurred_at)
        .select_from(NotificationDelivery)
        .join(previous, previous.id == NotificationDelivery.earthquake_id)
        .join(current, current.id == earthquake_id)
        .where(
            NotificationDelivery.subscription_id == subscription_id,
            NotificationDelivery.status == DeliveryStatus.SENT,
            NotificationDelivery.earthquake_id != earthquake_id,
            # A test quake is never "the same event" as a real one, either way round.
            previous.is_synthetic == current.is_synthetic,
            previous.occurred_at.between(
                current.occurred_at - config.duplicate_window,
                current.occurred_at + config.duplicate_window,
            ),
            func.ST_DWithin(previous.location, current.location, config.duplicate_distance_m),
        )
        .order_by(NotificationDelivery.sent_at.desc())
        .limit(1)
    )
    return occurred_at


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    delivery_id: uuid.UUID,
    status: DeliveryStatus,
    error: str | None,
    *,
    tried: bool = False,
    sent_at: datetime | None = None,
) -> None:
    async with session_factory() as session, session.begin():
        await _update(session, delivery_id, status, error, tried=tried, sent_at=sent_at)


async def _update(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    status: DeliveryStatus,
    error: str | None,
    *,
    tried: bool,
    sent_at: datetime | None = None,
) -> None:
    values: dict[str, object] = {"status": status, "last_error": error}
    if tried:
        values["attempts"] = NotificationDelivery.attempts + 1
    if sent_at is not None:
        values["sent_at"] = sent_at
    await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == DeliveryStatus.PENDING,
        )
        .values(**values)
    )
