"""webhook subscriptions: encrypted signing secret, consecutive failures

webhook_secret_hash becomes webhook_secret_encrypted: an HMAC key must be readable to sign,
so it is stored Fernet-encrypted rather than hashed. No webhook rows exist yet.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "subscriptions", "webhook_secret_hash", new_column_name="webhook_secret_encrypted"
    )
    op.add_column(
        "subscriptions",
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_subscriptions_webhook_fields"),
        "subscriptions",
        "channel <> 'webhook' OR (webhook_url IS NOT NULL"
        " AND webhook_secret_encrypted IS NOT NULL AND manage_token_hash IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_subscriptions_webhook_fields"), "subscriptions", type_="check")
    op.drop_column("subscriptions", "consecutive_failures")
    op.alter_column(
        "subscriptions", "webhook_secret_encrypted", new_column_name="webhook_secret_hash"
    )
