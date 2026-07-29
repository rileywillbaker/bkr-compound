"""Cost optimization: generic TTL cache table.

Backs sentinel/data/cache.py — provider-data freshness markers (so ingestion
skips calls for facts that haven't changed) and fingerprinted LLM reviews (so
an unchanged situation is never re-analyzed at cost). Purely additive: nothing
existing reads or writes this table, and every row is safe to delete.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cache_entries",
        sa.Column("key", sa.String(160), primary_key=True),
        sa.Column("kind", sa.String(48), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_cache_entries_kind", "cache_entries", ["kind"])
    op.create_index("ix_cache_entries_expires_at", "cache_entries", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_cache_entries_expires_at", table_name="cache_entries")
    op.drop_index("ix_cache_entries_kind", table_name="cache_entries")
    op.drop_table("cache_entries")
