"""index (occurred_at DESC, id DESC) for keyset pagination

Replaces ix_earthquakes_occurred_at: the API pages on (occurred_at DESC, id DESC), and the
composite index serves both that and plain newest-first ordering.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_earthquakes_occurred_at_id",
        "earthquakes",
        [sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.drop_index("ix_earthquakes_occurred_at", table_name="earthquakes")


def downgrade() -> None:
    op.create_index("ix_earthquakes_occurred_at", "earthquakes", [sa.text("occurred_at DESC")])
    op.drop_index("ix_earthquakes_occurred_at_id", table_name="earthquakes")
