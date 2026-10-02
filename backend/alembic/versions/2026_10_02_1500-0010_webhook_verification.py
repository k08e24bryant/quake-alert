"""webhook ownership verification: subscriptions.verified_at

A webhook subscription is active only after its receiver echoed a verification challenge.
Existing webhook rows never proved ownership, so they become pending (inactive): the owner
can verify them with POST /v1/subscriptions/webhook/{id}/verify, and the daily prune
deletes those still pending a day after they were created.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02 15:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE subscriptions SET is_active = false WHERE channel = 'webhook'")
    op.create_check_constraint(
        op.f("ck_subscriptions_webhook_verified"),
        "subscriptions",
        "channel <> 'webhook' OR verified_at IS NOT NULL OR NOT is_active",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_subscriptions_webhook_verified"), "subscriptions", type_="check")
    op.drop_column("subscriptions", "verified_at")
