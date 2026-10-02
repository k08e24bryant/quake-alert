"""The one interface every channel implements, and what the delivery pipeline hands it.

The pipeline (app.notifications.dispatcher) owns everything channels share: loading the
delivery, the freshness re-check before every attempt, retries, deactivation and
bookkeeping. A notifier only turns one alert into one send attempt for one recipient and
reports the result by returning (sent) or raising one of the three NotifierError kinds.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.db.models import SubscriptionChannel


@dataclass(frozen=True, slots=True)
class Recipient:
    subscription_id: uuid.UUID
    channel: SubscriptionChannel
    telegram_chat_id: int | None = None
    webhook_url: str | None = None
    webhook_secret_encrypted: str | None = None


@dataclass(frozen=True, slots=True)
class AlertData:
    """One quake as alerts show it: BMKG's values as stored (never modified), plus the
    distance from the recipient computed by PostGIS."""

    delivery_id: uuid.UUID
    earthquake_id: uuid.UUID
    occurred_at: datetime
    magnitude: Decimal
    depth_km: int
    latitude: float
    longitude: float
    region: str
    potential: str | None
    felt: str | None
    shakemap_url: str | None
    source_feeds: list[str]
    distance_km: float
    # Origin time of another row this recipient was already alerted about that is probably
    # the same event (dedup keeps such duplicates on purpose).
    possible_duplicate_of: datetime | None
    is_synthetic: bool


class NotifierError(Exception):
    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


class RetryableNotifierError(NotifierError):
    """Network error, timeout, 5xx, 429: a later attempt may succeed. `retry_after`
    overrides the exponential backoff when the channel says how long to wait (Telegram 429,
    webhook Retry-After). `rate_limited` means the recipient only asked us to slow down: a
    delivery that ends failed that way does not count towards deactivating a webhook."""

    def __init__(
        self, description: str, *, retry_after: float | None = None, rate_limited: bool = False
    ) -> None:
        super().__init__(description)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


class RecipientGoneError(NotifierError):
    """The recipient is gone for good (Telegram 403, webhook 410): deactivate, no retry."""


class PermanentNotifierError(NotifierError):
    """Retrying won't help (other 4xx, unsafe webhook target, bad config): fail now."""


class Notifier(Protocol):
    async def send(self, recipient: Recipient, alert: AlertData) -> None:
        """Deliver once. Return normally when the channel accepted it."""
        ...
