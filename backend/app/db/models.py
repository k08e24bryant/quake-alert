"""ORM models. Alembic autogenerate reads Base.metadata, so every model must live here
(or be imported here)."""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geography, WKBElement, WKTElement
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.ingestion.domain import Feed


class IngestionStatus(StrEnum):
    SUCCESS = "success"
    SKIPPED = "skipped"  # content hash equal to the last successful run; nothing processed
    FAILED = "failed"


class SubscriptionChannel(StrEnum):
    TELEGRAM = "telegram"
    WEBHOOK = "webhook"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"  # gave up: permanent error, retries exhausted, expired or unsubscribed


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class Earthquake(Base):
    __tablename__ = "earthquakes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    magnitude: Mapped[Decimal] = mapped_column(Numeric(3, 1))
    depth_km: Mapped[int] = mapped_column(Integer)
    # Reads come back as WKBElement; writes may pass WKTElement.
    location: Mapped[WKBElement | WKTElement] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    region: Mapped[str] = mapped_column(Text)
    # BMKG's free-text "Potensi", verbatim. Must never be labeled as tsunami information.
    potential: Mapped[str | None] = mapped_column(Text)
    felt: Mapped[str | None] = mapped_column(Text)
    shakemap_url: Mapped[str | None] = mapped_column(Text)
    source_feeds: Mapped[list[str]] = mapped_column(ARRAY(Text))
    fingerprint: Mapped[str] = mapped_column(Text, unique=True)
    # Latest raw item per feed, keyed by feed name.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Transactional outbox for alert matching: set in the same transaction as an insert or
    # a derived-field change, cleared in the same transaction that creates its deliveries.
    needs_matching: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Inserted by scripts/dev_fake_quake.py (dev only). Never in the public API, never a
    # dedup match, and its alerts are labelled as a test.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_earthquakes_location", "location", postgresql_using="gist"),
        # Newest-first listing and keyset pagination on (occurred_at DESC, id DESC).
        Index("ix_earthquakes_occurred_at_id", text("occurred_at DESC"), text("id DESC")),
        Index("ix_earthquakes_magnitude", "magnitude"),
        # Serves the same-feed revision lookup: raw @> {"<feed>": {"DateTime": "..."}}.
        Index(
            "ix_earthquakes_raw",
            "raw",
            postgresql_using="gin",
            postgresql_ops={"raw": "jsonb_path_ops"},
        ),
        # Only the few rows waiting for matching are in it.
        Index(
            "ix_earthquakes_needs_matching",
            "occurred_at",
            postgresql_where=text("needs_matching"),
        ),
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    feed: Mapped[Feed] = mapped_column(Enum(Feed, name="bmkg_feed", values_callable=_enum_values))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[IngestionStatus] = mapped_column(
        Enum(IngestionStatus, name="ingestion_status", values_callable=_enum_values)
    )
    content_hash: Mapped[str | None] = mapped_column(Text)
    inserted_count: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # Items not stored: malformed ones the parser dropped, plus items whose matching row has a
    # stored payload that no longer parses. Unrelated to the `skipped` status.
    skipped_count: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Serves "last successful run for this feed" (the content-hash skip check).
        Index("ix_ingestion_runs_feed_fetched_at", "feed", text("fetched_at DESC")),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    channel: Mapped[SubscriptionChannel] = mapped_column(
        Enum(SubscriptionChannel, name="subscription_channel", values_callable=_enum_values)
    )
    # One subscription per Telegram chat: sharing a new location moves it.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    # The HMAC signing secret, Fernet-encrypted with WEBHOOK_SECRET_KEYS: it must be readable
    # to sign, so it is encrypted, not hashed.
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    # sha256 of the manage token (a random 256-bit token, so a fast hash is enough).
    manage_token_hash: Mapped[str | None] = mapped_column(Text)
    # Failed webhook deliveries in a row; reset by a success. Unused for Telegram.
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    # Rounded to 2 decimals (~1 km) before it is stored, for the subscriber's privacy.
    location: Mapped[WKBElement | WKTElement] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    radius_km: Mapped[int] = mapped_column(Integer)
    min_magnitude: Mapped[Decimal] = mapped_column(Numeric(3, 1))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_subscriptions_location", "location", postgresql_using="gist"),
        CheckConstraint("radius_km > 0", name="radius_km_positive"),
        CheckConstraint(
            "channel <> 'telegram' OR telegram_chat_id IS NOT NULL", name="telegram_chat_id"
        ),
        CheckConstraint(
            "channel <> 'webhook' OR (webhook_url IS NOT NULL"
            " AND webhook_secret_encrypted IS NOT NULL AND manage_token_hash IS NOT NULL)",
            name="webhook_fields",
        ),
    )


class NotificationDelivery(Base):
    """One alert for one (subscription, earthquake row). The unique pair makes notifying
    idempotent: re-matching the same row never creates a second delivery."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    earthquake_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquakes.id", ondelete="CASCADE")
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", values_callable=_enum_values),
        server_default=DeliveryStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("subscription_id", "earthquake_id"),
        Index("ix_notification_deliveries_earthquake_id", "earthquake_id"),
    )
