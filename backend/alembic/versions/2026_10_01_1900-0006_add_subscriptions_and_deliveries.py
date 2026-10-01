"""add subscriptions and notification_deliveries

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 19:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

subscription_channel = postgresql.ENUM(
    "telegram", "webhook", name="subscription_channel", create_type=False
)
delivery_status = postgresql.ENUM(
    "pending", "sent", "failed", name="delivery_status", create_type=False
)


def upgrade() -> None:
    subscription_channel.create(op.get_bind())
    delivery_status.create(op.get_bind())

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("channel", subscription_channel, nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_secret_hash", sa.Text(), nullable=True),
        sa.Column("manage_token_hash", sa.Text(), nullable=True),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("radius_km", sa.Integer(), nullable=False),
        sa.Column("min_magnitude", sa.Numeric(precision=3, scale=1), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("radius_km > 0", name=op.f("ck_subscriptions_radius_km_positive")),
        sa.CheckConstraint(
            "channel <> 'telegram' OR telegram_chat_id IS NOT NULL",
            name=op.f("ck_subscriptions_telegram_chat_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint("telegram_chat_id", name=op.f("uq_subscriptions_telegram_chat_id")),
    )
    op.create_index(
        "ix_subscriptions_location", "subscriptions", ["location"], postgresql_using="gist"
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("earthquake_id", sa.Uuid(), nullable=False),
        sa.Column("status", delivery_status, server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["earthquake_id"],
            ["earthquakes.id"],
            name=op.f("fk_notification_deliveries_earthquake_id_earthquakes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_notification_deliveries_subscription_id_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.UniqueConstraint(
            "subscription_id",
            "earthquake_id",
            name=op.f("uq_notification_deliveries_subscription_id"),
        ),
    )
    op.create_index(
        "ix_notification_deliveries_earthquake_id", "notification_deliveries", ["earthquake_id"]
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("subscriptions")
    delivery_status.drop(op.get_bind())
    subscription_channel.drop(op.get_bind())
