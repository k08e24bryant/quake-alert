"""add earthquakes and ingestion_runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01 15:17:30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

bmkg_feed = postgresql.ENUM("autogempa", "gempaterkini", "gempadirasakan", name="bmkg_feed")
ingestion_status = postgresql.ENUM("success", "skipped", "failed", name="ingestion_status")


def upgrade() -> None:
    op.create_table(
        "earthquakes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("magnitude", sa.Numeric(precision=3, scale=1), nullable=False),
        sa.Column("depth_km", sa.Integer(), nullable=False),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("tsunami_potential", sa.Text(), nullable=True),
        sa.Column("felt", sa.Text(), nullable=True),
        sa.Column("shakemap_url", sa.Text(), nullable=True),
        sa.Column("source_feeds", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_earthquakes")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_earthquakes_fingerprint")),
    )
    op.create_index("ix_earthquakes_location", "earthquakes", ["location"], postgresql_using="gist")
    op.create_index("ix_earthquakes_occurred_at", "earthquakes", [sa.text("occurred_at DESC")])
    op.create_index("ix_earthquakes_magnitude", "earthquakes", ["magnitude"])

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("feed", bmkg_feed, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", ingestion_status, nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("inserted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_runs")),
    )
    op.create_index(
        "ix_ingestion_runs_feed_fetched_at",
        "ingestion_runs",
        ["feed", sa.text("fetched_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("ingestion_runs")
    bmkg_feed.drop(op.get_bind())
    ingestion_status.drop(op.get_bind())
    op.drop_table("earthquakes")
