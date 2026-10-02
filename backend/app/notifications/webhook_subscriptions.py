"""Webhook subscriptions: created through the API, managed with a token shown once.

- The signing secret is stored Fernet-encrypted (it must be readable to sign).
- The manage token is stored as sha256: it is 256 random bits, so a fast hash can't be
  brute-forced, and comparing hashes in constant time leaks nothing about it.
- Every authentication failure (no token, wrong token, unknown id, a Telegram
  subscription's id) looks the same to the caller: not found.

Lifecycle: pending_verification -> active -> inactive, never backwards.
- A new subscription is pending (inactive, so never matched) until its receiver echoes a
  verification challenge: tried once on creation, then again on each POST .../verify.
  Still pending a day later -> deleted by the daily prune.
- Active subscriptions are deactivated by a 410 or by consecutive failed deliveries.
  Nothing reactivates them: delete and create a new one.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from geoalchemy2 import WKTElement
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription, SubscriptionChannel
from app.notifications.base import Recipient
from app.notifications.subscriptions import round_coordinate
from app.notifications.webhook import VerificationResult, WebhookNotifier

SECRET_PREFIX = "whsec_"  # noqa: S105  (a prefix, not a secret)
MANAGE_TOKEN_PREFIX = "qamt_"  # noqa: S105  (a prefix, not a secret)


class WebhookChannelUnavailableError(Exception):
    """WEBHOOK_SECRET_KEYS is not set (only possible outside production)."""


class WebhookStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    INACTIVE = "inactive"


def status_of(row: Subscription) -> WebhookStatus:
    if row.verified_at is None:
        return WebhookStatus.PENDING_VERIFICATION
    return WebhookStatus.ACTIVE if row.is_active else WebhookStatus.INACTIVE


@dataclass(frozen=True, slots=True)
class Verification:
    status: WebhookStatus  # after this attempt
    result: VerificationResult


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
    verification: Verification


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
    now: datetime,
) -> CreatedWebhookSubscription:
    """Store the subscription as pending, then try to verify it once. The credentials are
    returned either way: the owner needs the manage token to retry the verification.

    Raises UnsafeTargetError / DnsResolutionError for a URL that may not be called."""
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
        is_active=False,  # until verified
        verified_at=None,
    )
    async with session.begin():
        session.add(row)
        await session.flush()
    verification = await verify_webhook_subscription(session, notifier, row, now=now)
    return CreatedWebhookSubscription(
        id=row.id,
        url=url,
        latitude=lat,
        longitude=lon,
        radius_km=radius_km,
        min_magnitude=min_magnitude,
        signing_secret=signing_secret,
        manage_token=manage_token,
        verification=verification,
    )


class NotPendingVerificationError(Exception):
    """Only a pending subscription can be verified; an inactive one is never reactivated."""

    def __init__(self, status: WebhookStatus) -> None:
        super().__init__(status.value)
        self.status = status


class SubscriptionGoneError(Exception):
    """The subscription was deleted while its receiver was being verified."""


async def verify_webhook_subscription(
    session: AsyncSession, notifier: WebhookNotifier, row: Subscription, *, now: datetime
) -> Verification:
    """Send a challenge to a pending subscription's receiver; activate it if echoed.

    Raises NotPendingVerificationError for a subscription that is not pending, and
    SubscriptionGoneError if it was deleted while waiting on the receiver."""
    if (status := status_of(row)) is not WebhookStatus.PENDING_VERIFICATION:
        raise NotPendingVerificationError(status)
    recipient = recipient_of(row)
    await session.close()  # don't hold a connection while waiting on the receiver
    result = await notifier.verify(recipient)
    if not result.verified:
        return Verification(WebhookStatus.PENDING_VERIFICATION, result)
    async with session.begin():
        activated = await session.scalar(
            update(Subscription)
            # Only from pending: never resurrects a row deleted or changed meanwhile.
            .where(Subscription.id == recipient.subscription_id, Subscription.verified_at.is_(None))
            .values(verified_at=now, is_active=True)
            .returning(Subscription.id)
        )
    if activated is None:  # deleted, or verified by a concurrent request, meanwhile
        refreshed = await session.get(Subscription, recipient.subscription_id)
        if refreshed is None:
            raise SubscriptionGoneError
        return Verification(status_of(refreshed), result)
    return Verification(WebhookStatus.ACTIVE, result)


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
