"""rename tsunami_potential to potential, add ingestion_runs.skipped_count

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 16:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # BMKG's "Potensi" is free text and not always about tsunamis. A rename keeps the data.
    op.alter_column("earthquakes", "tsunami_potential", new_column_name="potential")
    op.add_column(
        "ingestion_runs",
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ingestion_runs", "skipped_count")
    op.alter_column("earthquakes", "potential", new_column_name="tsunami_potential")
