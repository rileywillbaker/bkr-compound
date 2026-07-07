"""Signal lifecycle + alerts: signals, risk_checks, alerts, journal_entries.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("run_id", sa.String(36), nullable=False, server_default="", index=True),
        sa.Column("ticker", sa.String(12), nullable=False, index=True),
        sa.Column("action", sa.String(8), nullable=False, index=True),
        sa.Column("shares", sa.Integer),
        sa.Column("max_entry_price", sa.Numeric(18, 6)),
        sa.Column("stop_loss", sa.Numeric(18, 6)),
        sa.Column("take_profit", sa.Numeric(18, 6)),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("expected_return_pct", sa.Float),
        sa.Column("risk_score", sa.Integer, nullable=False),
        sa.Column("time_horizon", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(32), nullable=False, index=True),
        sa.Column("regime", sa.String(24), nullable=False, index=True),
        sa.Column("evidence", sa.JSON),
        sa.Column("explanation", sa.Text, server_default=""),
        sa.Column("deterministic_only", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("alert_sent", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("user_decision", sa.String(12), index=True),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "risk_checks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.String(36), nullable=False, index=True),
        sa.Column("approved", sa.Boolean, nullable=False, index=True),
        sa.Column("profile_version", sa.Integer, nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rules", sa.JSON, nullable=False),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("kind", sa.String(24), nullable=False, index=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="telegram"),
        sa.Column("signal_id", sa.String(36), index=True),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("text", sa.Text, server_default=""),
        sa.Column("detail", sa.Text, server_default=""),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("signal_id", sa.String(36), nullable=False, index=True),
        sa.Column("ticker", sa.String(12), nullable=False, index=True),
        sa.Column("decision", sa.String(12), nullable=False),
        sa.Column("note", sa.Text, server_default=""),
    )


def downgrade() -> None:
    for table in ("journal_entries", "alerts", "risk_checks", "signals"):
        op.drop_table(table)
