"""Market intelligence: short_interest (Fintel).

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "short_interest",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("as_of", sa.Date, nullable=False),
        sa.Column("short_percent_float", sa.Float),
        sa.Column("short_percent_shares_outstanding", sa.Float),
        sa.Column("days_to_cover", sa.Float),
        sa.Column("short_interest_change_pct", sa.Float),
        sa.Column("dark_pool_short_volume_pct", sa.Float),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("short_interest")
