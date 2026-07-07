"""Portfolio + risk tables: positions, trades, equity_snapshots, risk_profiles.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("shares", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_basis", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("symbol", sa.String(12), nullable=False, index=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("shares", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("signal_id", sa.String(36), index=True),
        sa.Column("note", sa.Text, server_default=""),
    )
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("equity", sa.Float, nullable=False),
    )
    op.create_table(
        "risk_profiles",
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("risk_profiles", "equity_snapshots", "trades", "positions"):
        op.drop_table(table)
