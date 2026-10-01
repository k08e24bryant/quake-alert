"""enable postgis extension

Revision ID: 0001
Revises:
Create Date: 2026-10-01 14:50:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    # No CASCADE: if other extensions or columns depend on PostGIS, fail loudly
    # rather than silently dropping them.
    op.execute("DROP EXTENSION IF EXISTS postgis")
