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
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    Text,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_earthquakes_location", "location", postgresql_using="gist"),
        Index("ix_earthquakes_occurred_at", text("occurred_at DESC")),
        Index("ix_earthquakes_magnitude", "magnitude"),
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
    # Feed items the parser dropped as malformed (unrelated to the `skipped` status).
    skipped_count: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Serves "last successful run for this feed" (the content-hash skip check).
        Index("ix_ingestion_runs_feed_fetched_at", "feed", text("fetched_at DESC")),
    )
