"""Webhook subscriptions: created through the API, managed with a token shown once.

- The signing secret is stored Fernet-encrypted (it must be readable to sign).
- The manage token is stored as sha256: it is 256 random bits, so a fast hash can't be
  brute-forced, and comparing hashes in constant time leaks nothing about it.
- Every authentication failure (no token, wrong token, unknown id, a Telegram
  subscription's id) looks the same to the caller: not found.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from decimal import Decimal

from geoalchemy2 import WKTElement
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription, SubscriptionChannel
from app.notifications.base import Recipient
from app.notifications.subscriptions import round_coordinate
from app.notifications.webhook import WebhookNotifier

SECRET_PREFIX = "whsec_"  # noqa: S105  (a prefix, not a secret)
MANAGE_TOKEN_PREFIX = "qamt_"  # noqa: S105  (a prefix, not a secret)


class WebhookChannelUnavailableError(Exception):
    """WEBHOOK_SECRET_KEYS is not set (only possible outside production)."""


@dataclass(frozen=True, slots=True)
class CreatedWebhookSubscription:
    id: uuid.UUID
    url: str
    latitude: Decimal
    longitude: Decimal
    radius_km: int
    min_magnitude: Decimal
    signing_secret: str
    manage_token: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_webhook_subscription(
    session: AsyncSession,
    notifier: WebhookNotifier,
    *,
    url: str,
    latitude: float,
    longitude: float,
    radius_km: int,
    min_magnitude: Decimal,
) -> CreatedWebhookSubscription:
    """Raises UnsafeTargetError / DnsResolutionError for a URL that may not be called."""
    box = notifier.secret_box
    if box is None:
        raise WebhookChannelUnavailableError
    # Checked now so obvious mistakes fail early; the check that matters runs at send time.
    await notifier.check_url(url)
    signing_secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    manage_token = MANAGE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    lat, lon = round_coordinate(latitude), round_coordinate(longitude)
    row = Subscription(
        channel=SubscriptionChannel.WEBHOOK,
        webhook_url=url,
        webhook_secret_encrypted=box.encrypt(signing_secret),
        manage_token_hash=hash_token(manage_token),
        location=WKTElement(f"POINT({lon} {lat})", srid=4326),
        radius_km=radius_km,
        min_magnitude=min_magnitude,
        is_active=True,
    )
    async with session.begin():
        session.add(row)
        await session.flush()
    return CreatedWebhookSubscription(
        id=row.id,
        url=url,
        latitude=lat,
        longitude=lon,
        radius_km=radius_km,
        min_magnitude=min_magnitude,
        signing_secret=signing_secret,
        manage_token=manage_token,
    )


async def authenticate(
    session: AsyncSession, subscription_id: uuid.UUID, manage_token: str | None
) -> Subscription | None:
    """The webhook subscription, if `manage_token` is its token; otherwise None."""
    row = await session.scalar(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.channel == SubscriptionChannel.WEBHOOK,
        )
    )
    # Hash and compare even when there is nothing to match, so a miss costs the same.
    presented = hash_token(manage_token or "")
    expected = row.manage_token_hash if row is not None and row.manage_token_hash else ""
    if manage_token and expected and hmac.compare_digest(presented, expected):
        return row
    return None


async def delete_webhook_subscription(
    session: AsyncSession, subscription_id: uuid.UUID, manage_token: str | None
) -> bool:
    """Hard delete; deliveries go with it (ON DELETE CASCADE). False = not found / denied."""
    async with session.begin():
        row = await authenticate(session, subscription_id, manage_token)
        if row is None:
            return False
        await session.execute(delete(Subscription).where(Subscription.id == row.id))
    return True


def recipient_of(row: Subscription) -> Recipient:
    return Recipient(
        subscription_id=row.id,
        channel=row.channel,
        webhook_url=row.webhook_url,
        webhook_secret_encrypted=row.webhook_secret_encrypted,
    )
