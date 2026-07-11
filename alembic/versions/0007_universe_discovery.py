"""Universe expansion + news-triggered discovery: insider_transactions.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "insider_transactions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(12), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("share_change", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("transaction_date", sa.Date, nullable=False, index=True),
        sa.Column("transaction_price", sa.Float),
        sa.Column("filing_date", sa.Date),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "symbol", "name", "transaction_date", "share_change", name="uq_insider_txn"
        ),
    )


def downgrade() -> None:
    op.drop_table("insider_transactions")
