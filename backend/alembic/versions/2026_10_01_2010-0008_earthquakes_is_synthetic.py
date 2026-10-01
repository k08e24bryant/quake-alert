"""earthquakes.is_synthetic: dev-only test quakes (scripts/dev_fake_quake.py)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01 20:10:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "earthquakes",
        sa.Column("is_synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("earthquakes", "is_synthetic")
