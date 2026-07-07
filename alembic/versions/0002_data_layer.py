"""Data-layer tables: bars (hypertable), quotes, news, macro, fundamentals,
earnings, filings, provider_credentials, system_events, api_usage.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bars",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("timeframe", sa.String(8), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("open", sa.Numeric(18, 6), nullable=False),
        sa.Column("high", sa.Numeric(18, 6), nullable=False),
        sa.Column("low", sa.Numeric(18, 6), nullable=False),
        sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
    )
    op.execute(
        "SELECT create_hypertable('bars', 'ts', if_not_exists => TRUE, "
        "migrate_data => TRUE)"
    )

    op.create_table(
        "quotes_latest",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bid", sa.Numeric(18, 6)),
        sa.Column("ask", sa.Numeric(18, 6)),
        sa.Column("last", sa.Numeric(18, 6)),
    )

    op.create_table(
        "news_items",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.String(64), nullable=False, index=True),
        sa.Column("symbol", sa.String(12), index=True),
        sa.Column("headline", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, server_default=""),
        sa.Column("source", sa.String(128), server_default=""),
        sa.Column("url", sa.Text, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_id", "symbol", name="uq_news_provider_symbol"),
    )

    op.create_table(
        "macro_series",
        sa.Column("series_id", sa.String(32), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("value", sa.Float),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "fundamentals",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(256), server_default=""),
        sa.Column("sector", sa.String(128), server_default="", index=True),
        sa.Column("market_cap", sa.Float),
        sa.Column("exchange", sa.String(32), server_default=""),
        sa.Column("pe", sa.Float),
        sa.Column("ps", sa.Float),
        sa.Column("eps_growth_ttm", sa.Float),
        sa.Column("revenue_growth_ttm", sa.Float),
        sa.Column("beta", sa.Float),
        sa.Column("week52_high", sa.Float),
        sa.Column("week52_low", sa.Float),
    )

    op.create_table(
        "earnings_calendar",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("hour", sa.String(8), server_default=""),
        sa.Column("eps_estimate", sa.Float),
        sa.Column("eps_actual", sa.Float),
        sa.Column("revenue_estimate", sa.Float),
        sa.Column("revenue_actual", sa.Float),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "filings",
        sa.Column("accession_no", sa.String(32), primary_key=True),
        sa.Column("symbol", sa.String(12), nullable=False, index=True),
        sa.Column("cik", sa.String(16), nullable=False, index=True),
        sa.Column("form", sa.String(12), nullable=False, index=True),
        sa.Column("filed_at", sa.Date, nullable=False, index=True),
        sa.Column("url", sa.Text, server_default=""),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("encrypted_value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "field", name="uq_credential"),
    )

    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("level", sa.String(8), server_default="INFO"),
        sa.Column("kind", sa.String(64), nullable=False, index=True),
        sa.Column("message", sa.Text, server_default=""),
        sa.Column("payload", sa.JSON),
    )

    op.create_table(
        "api_usage",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("provider", sa.String(32), nullable=False, index=True),
        sa.Column("endpoint", sa.String(128), server_default=""),
        sa.Column("tokens_in", sa.Integer, server_default="0"),
        sa.Column("tokens_out", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Float, server_default="0"),
        sa.Column("ok", sa.Boolean, server_default=sa.true()),
        sa.Column("detail", sa.Text, server_default=""),
    )


def downgrade() -> None:
    for table in (
        "api_usage",
        "system_events",
        "provider_credentials",
        "filings",
        "earnings_calendar",
        "fundamentals",
        "macro_series",
        "news_items",
        "quotes_latest",
        "bars",
    ):
        op.drop_table(table)
