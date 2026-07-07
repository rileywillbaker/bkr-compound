"""Evaluation loop: evaluations + strategy_stats.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.String(36), nullable=False, unique=True, index=True),
        sa.Column("ticker", sa.String(12), nullable=False, index=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False, index=True),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("exit_price", sa.Float, nullable=False),
        sa.Column("r_multiple", sa.Float, nullable=False),
        sa.Column("return_pct", sa.Float, nullable=False),
        sa.Column("win", sa.Boolean, nullable=False, index=True),
        sa.Column("holding_days", sa.Integer, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("strategy", sa.String(32), nullable=False, index=True),
        sa.Column("regime", sa.String(24), nullable=False, index=True),
        sa.Column("user_decision", sa.String(12)),
    )
    op.create_table(
        "strategy_stats",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("strategy", sa.String(32), nullable=False, index=True),
        sa.Column("regime", sa.String(24), nullable=False, server_default="*", index=True),
        sa.Column("resolved_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("hit_rate", sa.Float, nullable=False, server_default="0"),
        sa.Column("expectancy_r", sa.Float, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("strategy", "regime", name="uq_strategy_regime"),
    )


def downgrade() -> None:
    op.drop_table("strategy_stats")
    op.drop_table("evaluations")
