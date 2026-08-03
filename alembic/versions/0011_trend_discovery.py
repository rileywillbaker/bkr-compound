"""Trend Discovery Agent: free-source document store, ETF holdings snapshots,
social aggregates, per-theme trend snapshots, and the daily trend report.

Purely additive. Nothing existing reads or writes these tables, and every row
is safe to delete — the agent just rebuilds its history from the next
collection run onwards.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trend_documents",
        sa.Column("doc_key", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("author", sa.String(128), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("themes", sa.JSON(), nullable=True),
        sa.Column("symbols", sa.JSON(), nullable=True),
        sa.Column("sentiment", sa.Float(), nullable=False, server_default="0"),
        sa.Column("engagement", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_trend_documents_source", "trend_documents", ["source"])
    op.create_index("ix_trend_documents_channel", "trend_documents", ["channel"])
    op.create_index("ix_trend_documents_published_at", "trend_documents", ["published_at"])

    op.create_table(
        "etf_holdings",
        sa.Column("etf", sa.String(12), primary_key=True),
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("as_of", sa.Date(), primary_key=True),
        sa.Column("weight_pct", sa.Float(), nullable=True),
        sa.Column("shares", sa.Float(), nullable=True),
        sa.Column("market_value", sa.Float(), nullable=True),
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_etf_holdings_symbol", "etf_holdings", ["symbol"])

    op.create_table(
        "social_mentions",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("source", sa.String(24), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("mentions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sentiment", sa.Float(), nullable=False, server_default="0"),
        sa.Column("positive", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("negative", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engagement", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_social_mentions_symbol", "social_mentions", ["symbol"])
    op.create_index("ix_social_mentions_day", "social_mentions", ["day"])

    op.create_table(
        "trend_snapshots",
        sa.Column("theme", sa.String(48), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("legitimacy", sa.String(16), nullable=False, server_default="unproven"),
        sa.Column("components", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("symbols", sa.JSON(), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_trend_snapshots_day", "trend_snapshots", ["day"])

    op.create_table(
        "trend_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "market_environment", sa.String(16), nullable=False, server_default="Neutral"
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("llm_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("alert_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_trend_reports_day", "trend_reports", ["day"])


def downgrade() -> None:
    op.drop_index("ix_trend_reports_day", table_name="trend_reports")
    op.drop_table("trend_reports")
    op.drop_index("ix_trend_snapshots_day", table_name="trend_snapshots")
    op.drop_table("trend_snapshots")
    op.drop_index("ix_social_mentions_day", table_name="social_mentions")
    op.drop_index("ix_social_mentions_symbol", table_name="social_mentions")
    op.drop_table("social_mentions")
    op.drop_index("ix_etf_holdings_symbol", table_name="etf_holdings")
    op.drop_table("etf_holdings")
    op.drop_index("ix_trend_documents_published_at", table_name="trend_documents")
    op.drop_index("ix_trend_documents_channel", table_name="trend_documents")
    op.drop_index("ix_trend_documents_source", table_name="trend_documents")
    op.drop_table("trend_documents")
