"""Send one delivery (one alert to one subscriber) and record what happened: the single
delivery pipeline every channel goes through (app.notifications.base.Notifier).

Called by the worker's per-delivery arq job. Retrying is the job's business: this module
raises RetryDeliveryError with how long to wait, and gives up by itself on the final
attempt, so a delivery never stays `pending` after its last try.

Shared by every channel, before every attempt (first or retry): the delivery must still be
pending, the subscription active, the quake within notify_max_age_minutes, and a synthetic
quake is only ever sent in development. Then the channel's notifier tries once:
  - returns              -> sent (a webhook's consecutive failure count is reset)
  - RecipientGoneError   -> subscription deactivated, delivery failed, no retry
  - RetryableNotifierError -> retry after its retry_after or exponential backoff;
                            failed after the last attempt
  - PermanentNotifierError -> failed, no retry
A webhook subscription whose deliveries end `failed` max_consecutive_failures times in a
row is deactivated (logged once). Telegram subscriptions are never counted.

Delivery is at-least-once: if the channel accepted the alert but recording `sent` fails,
the retry sends it again. Per the safety principle a duplicate beats a missed alert.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Float, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.db.models import (
    DeliveryStatus,
    Earthquake,
    NotificationDelivery,
    Subscription,
    SubscriptionChannel,
)
from app.notifications.base import (
    AlertData,
    Notifier,
    PermanentNotifierError,
    Recipient,
    RecipientGoneError,
    RetryableNotifierError,
)
from app.notifications.subscriptions import deactivate
from app.notifications.telegram import TelegramClient, TelegramNotifier

logger = logging.getLogger(__name__)


class DeliveryOutcome(StrEnum):
    SENT = "sent"
    NOT_PENDING = "not_pending"  # deleted with its subscription (/stop), or already done
    EXPIRED = "expired"  # the quake is older than notify_max_age_minutes by now
    INACTIVE = "inactive"  # the subscription was deactivated after matching
    DEACTIVATED = "deactivated"  # the recipient is gone (Telegram 403, webhook 410)
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
    max_consecutive_failures: int = 10  # webhooks only
    # Synthetic (dev test) quakes are sent only when this is true: ENVIRONMENT=development.
    allow_synthetic: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "NotifyConfig":
        return cls(
            max_age=timedelta(minutes=settings.notify_max_age_minutes),
            max_attempts=settings.notify_max_attempts,
            retry_backoff_seconds=settings.notify_retry_backoff_seconds,
            duplicate_window=timedelta(seconds=settings.notify_duplicate_window_seconds),
            duplicate_distance_m=settings.notify_duplicate_distance_km * 1000,
            max_consecutive_failures=settings.webhook_max_consecutive_failures,
            allow_synthetic=settings.environment == "development",
        )

    def backoff_seconds(self, attempt: int) -> float:
        """Delay before the try after `attempt` (1-based)."""
        return self.retry_backoff_seconds * 2.0 ** (attempt - 1)


@dataclass(frozen=True, slots=True)
class _Pending:
    recipient: Recipient
    is_active: bool
    attempts: int  # attempts already made before this one
    alert: AlertData


async def deliver(
    session_factory: async_sessionmaker[AsyncSession],
    telegram: TelegramClient,
    delivery_id: uuid.UUID,
    *,
    attempt: int,
    now: datetime,
    config: NotifyConfig,
    webhook: Notifier | None = None,
) -> DeliveryOutcome:
    """Try to send the delivery once. `attempt` is 1-based. Raises RetryDeliveryError when
    a later attempt may succeed."""
    notifiers: Mapping[SubscriptionChannel, Notifier | None] = {
        SubscriptionChannel.TELEGRAM: TelegramNotifier(telegram),
        SubscriptionChannel.WEBHOOK: webhook,
    }
    async with session_factory() as session:
        pending = await _load(session, delivery_id, config)
    if pending is None:
        return DeliveryOutcome.NOT_PENDING
    recipient = pending.recipient
    if not pending.is_active or not _has_address(recipient):
        await _record(session_factory, delivery_id, DeliveryStatus.FAILED, "subscription inactive")
        return DeliveryOutcome.INACTIVE
    if pending.alert.occurred_at < now - config.max_age:
        # Not this attempt's fault, but if earlier attempts failed, the delivery failed.
        await _fail(
            session_factory, pending, delivery_id, "expired before sending", config, tried=False
        )
        return DeliveryOutcome.EXPIRED
    if pending.alert.is_synthetic and not config.allow_synthetic:
        await _record(
            session_factory, delivery_id, DeliveryStatus.FAILED, "synthetic quake outside dev"
        )
        logger.error("refused to send a synthetic quake", extra={"delivery_id": str(delivery_id)})
        return DeliveryOutcome.FAILED
    notifier = notifiers[recipient.channel]
    if notifier is None:
        error = f"{recipient.channel.value} channel is not configured in this worker"
        await _record(session_factory, delivery_id, DeliveryStatus.FAILED, error)
        return DeliveryOutcome.FAILED

    try:
        await notifier.send(recipient, pending.alert)
    except RecipientGoneError as exc:
        async with session_factory() as session, session.begin():
            await deactivate(session, recipient.subscription_id)
            await _update(session, delivery_id, DeliveryStatus.FAILED, exc.description, tried=True)
        logger.info(
            "recipient is gone; subscription deactivated",
            extra={
                "delivery_id": str(delivery_id),
                "subscription_id": str(recipient.subscription_id),
                "channel": recipient.channel.value,
            },
        )
        return DeliveryOutcome.DEACTIVATED
    except RetryableNotifierError as exc:
        if attempt >= config.max_attempts:
            return await _give_up(session_factory, pending, delivery_id, exc.description, config)
        await _record(
            session_factory, delivery_id, DeliveryStatus.PENDING, exc.description, tried=True
        )
        defer = exc.retry_after if exc.retry_after is not None else config.backoff_seconds(attempt)
        logger.warning(
            "alert delivery failed, will retry",
            extra={"delivery_id": str(delivery_id), "attempt": attempt, "retry_in_s": defer},
        )
        raise RetryDeliveryError(defer) from exc
    except PermanentNotifierError as exc:
        return await _give_up(session_factory, pending, delivery_id, exc.description, config)

    async with session_factory() as session, session.begin():
        await _update(session, delivery_id, DeliveryStatus.SENT, None, tried=True, sent_at=now)
        if recipient.channel is SubscriptionChannel.WEBHOOK:
            await session.execute(
                update(Subscription)
                .where(Subscription.id == recipient.subscription_id)
                .values(consecutive_failures=0)
            )
    return DeliveryOutcome.SENT


def _has_address(recipient: Recipient) -> bool:
    if recipient.channel is SubscriptionChannel.TELEGRAM:
        return recipient.telegram_chat_id is not None
    return bool(recipient.webhook_url and recipient.webhook_secret_encrypted)


async def _give_up(
    session_factory: async_sessionmaker[AsyncSession],
    pending: _Pending,
    delivery_id: uuid.UUID,
    error: str,
    config: NotifyConfig,
) -> DeliveryOutcome:
    await _fail(session_factory, pending, delivery_id, error, config, tried=True)
    logger.warning(
        "alert delivery failed permanently",
        extra={"delivery_id": str(delivery_id), "error": error},
    )
    return DeliveryOutcome.FAILED


async def _fail(
    session_factory: async_sessionmaker[AsyncSession],
    pending: _Pending,
    delivery_id: uuid.UUID,
    error: str,
    config: NotifyConfig,
    *,
    tried: bool,
) -> None:
    """Mark the delivery failed and, for a webhook whose endpoint was actually tried, count
    it towards deactivation, in one transaction."""
    recipient = pending.recipient
    async with session_factory() as session, session.begin():
        failed = await _update(session, delivery_id, DeliveryStatus.FAILED, error, tried=tried)
        endpoint_was_tried = tried or pending.attempts > 0
        if failed and recipient.channel is SubscriptionChannel.WEBHOOK and endpoint_was_tried:
            await _count_consecutive_failure(session, recipient.subscription_id, config)


async def _count_consecutive_failure(
    session: AsyncSession, subscription_id: uuid.UUID, config: NotifyConfig
) -> None:
    failures = await session.scalar(
        update(Subscription)
        .where(Subscription.id == subscription_id)
        .values(consecutive_failures=Subscription.consecutive_failures + 1)
        .returning(Subscription.consecutive_failures)
    )
    if failures is None or failures < config.max_consecutive_failures:
        return
    deactivated = await session.scalar(
        update(Subscription)
        .where(Subscription.id == subscription_id, Subscription.is_active)
        .values(is_active=False)
        .returning(Subscription.id)
    )
    if deactivated is not None:  # only the transition is logged, once
        logger.warning(
            "webhook subscription deactivated after consecutive failed deliveries",
            extra={"subscription_id": str(subscription_id), "failures": failures},
        )


async def _load(
    session: AsyncSession, delivery_id: uuid.UUID, config: NotifyConfig
) -> _Pending | None:
    distance_m = func.ST_Distance(Subscription.location, Earthquake.location, type_=Float)
    row = (
        await session.execute(
            select(
                NotificationDelivery.subscription_id,
                NotificationDelivery.earthquake_id,
                NotificationDelivery.attempts,
                Subscription.channel,
                Subscription.telegram_chat_id,
                Subscription.webhook_url,
                Subscription.webhook_secret_encrypted,
                Subscription.is_active,
                Earthquake.occurred_at,
                Earthquake.magnitude,
                Earthquake.depth_km,
                func.ST_Y(func.geometry(Earthquake.location), type_=Float).label("latitude"),
                func.ST_X(func.geometry(Earthquake.location), type_=Float).label("longitude"),
                Earthquake.region,
                Earthquake.potential,
                Earthquake.felt,
                Earthquake.shakemap_url,
                Earthquake.source_feeds,
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
    alert = AlertData(
        delivery_id=delivery_id,
        earthquake_id=row.earthquake_id,
        occurred_at=row.occurred_at,
        magnitude=row.magnitude,
        depth_km=row.depth_km,
        latitude=row.latitude,
        longitude=row.longitude,
        region=row.region,
        potential=row.potential,
        felt=row.felt,
        shakemap_url=row.shakemap_url,
        source_feeds=list(row.source_feeds),
        distance_km=row.distance_km,
        possible_duplicate_of=await _already_alerted_nearby(
            session, row.subscription_id, row.earthquake_id, config
        ),
        is_synthetic=row.is_synthetic,
    )
    recipient = Recipient(
        subscription_id=row.subscription_id,
        channel=row.channel,
        telegram_chat_id=row.telegram_chat_id,
        webhook_url=row.webhook_url,
        webhook_secret_encrypted=row.webhook_secret_encrypted,
    )
    return _Pending(recipient, row.is_active, row.attempts, alert)


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
) -> None:
    async with session_factory() as session, session.begin():
        await _update(session, delivery_id, status, error, tried=tried)


async def _update(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    status: DeliveryStatus,
    error: str | None,
    *,
    tried: bool,
    sent_at: datetime | None = None,
) -> bool:
    """Update a still-pending delivery; False if it was no longer pending."""
    values: dict[str, object] = {"status": status, "last_error": error}
    if tried:
        values["attempts"] = NotificationDelivery.attempts + 1
    if sent_at is not None:
        values["sent_at"] = sent_at
    updated = await session.scalar(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id == delivery_id,
            NotificationDelivery.status == DeliveryStatus.PENDING,
        )
        .values(**values)
        .returning(NotificationDelivery.id)
    )
    return updated is not None
