"""add GIN index on earthquakes.raw

Serves the same-feed revision lookup in dedup: raw @> {"<feed>": {"DateTime": "..."}}.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 17:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_earthquakes_raw",
        "earthquakes",
        ["raw"],
        postgresql_using="gin",
        postgresql_ops={"raw": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_earthquakes_raw", table_name="earthquakes")
