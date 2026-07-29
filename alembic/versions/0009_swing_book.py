"""Swing trading: `book` tag on signals (core vs swing).

Additive and backward-compatible: every pre-existing signal is backfilled to
"core", so the long-term feed is unchanged. The swing pipeline writes "swing".

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column("book", sa.String(8), nullable=False, server_default="core"),
    )
    # Backfill is redundant given the server_default (existing rows get "core"),
    # but explicit for clarity and to cover any driver that skips defaults.
    op.execute("UPDATE signals SET book = 'core' WHERE book IS NULL")
    op.create_index("ix_signals_book", "signals", ["book"])


def downgrade() -> None:
    op.drop_index("ix_signals_book", table_name="signals")
    op.drop_column("signals", "book")
