"""earthquakes.needs_matching: transactional outbox for alert matching

Set in the same transaction as an insert or derived-field change; the match job clears it
in the same transaction that creates the deliveries, so a lost enqueue can't lose a match.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01 20:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "earthquakes",
        sa.Column("needs_matching", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index(
        "ix_earthquakes_needs_matching",
        "earthquakes",
        ["occurred_at"],
        postgresql_where=sa.text("needs_matching"),
    )


def downgrade() -> None:
    op.drop_index("ix_earthquakes_needs_matching", table_name="earthquakes")
    op.drop_column("earthquakes", "needs_matching")
