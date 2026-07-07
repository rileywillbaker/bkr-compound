"""SQLAlchemy models (spec §8). Tables are added phase by phase; all
timestamps are stored UTC (timestamptz)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base

UTCNow = lambda: datetime.utcnow()  # noqa: E731


# --------------------------------------------------------------------------
# Phase 1 — market/reference data
# --------------------------------------------------------------------------
class BarRow(Base):
    """OHLCV bars. Converted to a Timescale hypertable in the migration."""

    __tablename__ = "bars"
    # Hypertables need the partition column in every unique constraint,
    # so the primary key is composite and there is no surrogate id.
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    volume: Mapped[int] = mapped_column(BigInteger)


class QuoteLatest(Base):
    __tablename__ = "quotes_latest"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    last: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)


class NewsItemRow(Base):
    __tablename__ = "news_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(12), index=True, nullable=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    __table_args__ = (UniqueConstraint("provider_id", "symbol", name="uq_news_provider_symbol"),)


class MacroSeriesRow(Base):
    __tablename__ = "macro_series"
    series_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)


class FundamentalsRow(Base):
    __tablename__ = "fundamentals"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    name: Mapped[str] = mapped_column(String(256), default="")
    sector: Mapped[str] = mapped_column(String(128), default="", index=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    exchange: Mapped[str] = mapped_column(String(32), default="")
    pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    ps: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_growth_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_growth_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    beta: Mapped[float | None] = mapped_column(Float, nullable=True)
    week52_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    week52_low: Mapped[float | None] = mapped_column(Float, nullable=True)


class EarningsCalendarRow(Base):
    __tablename__ = "earnings_calendar"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    hour: Mapped[str] = mapped_column(String(8), default="")
    eps_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)


class FilingRow(Base):
    __tablename__ = "filings"
    accession_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    cik: Mapped[str] = mapped_column(String(16), index=True)
    form: Mapped[str] = mapped_column(String(12), index=True)
    filed_at: Mapped[date] = mapped_column(Date, index=True)
    url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)


class ProviderCredential(Base):
    """Encrypted external API keys pasted via the Settings UI (spec addition)."""

    __tablename__ = "provider_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))
    field: Mapped[str] = mapped_column(String(32))
    encrypted_value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    __table_args__ = (UniqueConstraint("provider", "field", name="uq_credential"),)


class SystemEvent(Base):
    """Audit/ops log: ingestion runs, watchdog alerts, provider errors."""

    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")  # INFO/WARN/ERROR
    kind: Mapped[str] = mapped_column(String(64), index=True)  # e.g. ingest.bars
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# --------------------------------------------------------------------------
# Phase 2 — portfolio + risk
# --------------------------------------------------------------------------
class Position(Base):
    """Current holdings, maintained from user-entered trades."""

    __tablename__ = "positions"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0)  # per share
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)


class TradeRow(Base):
    """User-entered fills (manual). The system never creates these itself."""

    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    symbol: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[str] = mapped_column(String(4))  # BUY / SELL
    shares: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")


class EquitySnapshot(Base):
    """Point-in-time account equity; source of high-water mark and day P&L."""

    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    equity: Mapped[float] = mapped_column(Float)


class RiskProfileRow(Base):
    """Versioned risk profiles (append-only; newest version is active)."""

    __tablename__ = "risk_profiles"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    params: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)


# --------------------------------------------------------------------------
# Phase 4 — signal lifecycle + alerts
# --------------------------------------------------------------------------
class SignalRow(Base):
    """Persisted pipeline signals (spec §4 schema). Numeric fields were
    computed deterministically before this row was written; the LLM only ever
    contributed the explanation text and evidence citations."""

    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=UTCNow, index=True
    )
    run_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    action: Mapped[str] = mapped_column(String(8), index=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    expected_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_score: Mapped[int] = mapped_column(Integer)
    time_horizon: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    regime: Mapped[str] = mapped_column(String(24), index=True)
    evidence: Mapped[list | None] = mapped_column(JSON, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
    deterministic_only: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    user_decision: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiskCheckRow(Base):
    """Full rule-by-rule risk evaluation for a signal (spec §5: rejections are
    stored and visible; approvals too, for the audit trail)."""

    __tablename__ = "risk_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(36), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, index=True)
    profile_version: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    rules: Mapped[list] = mapped_column(JSON)


class AlertRow(Base):
    """Outbound alert log: signal alerts, briefs, test sends. Also the source
    of the max-alerts-per-day rate limit."""

    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)  # signal/brief_*/test
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    text: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")  # error info when not ok


class JournalEntryRow(Base):
    """Trade journal, auto-created from signals + user decisions (spec §7.5);
    free-text notes are user-editable."""

    __tablename__ = "journal_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    signal_id: Mapped[str] = mapped_column(String(36), index=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    decision: Mapped[str] = mapped_column(String(12))
    note: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------
# Phase 6 — evaluation loop
# --------------------------------------------------------------------------
class EvaluationRow(Base):
    """Resolved-signal outcome (spec §9). Every BUY signal with full trade
    parameters resolves exactly once — including ones the user skipped, so
    missed opportunities and false positives are both measured."""

    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    outcome: Mapped[str] = mapped_column(String(16), index=True)  # target_hit|stop_hit|expired
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    r_multiple: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)
    win: Mapped[bool] = mapped_column(Boolean, index=True)
    holding_days: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)  # denormalized for Brier
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    regime: Mapped[str] = mapped_column(String(24), index=True)
    user_decision: Mapped[str | None] = mapped_column(String(12), nullable=True)


class StrategyStatRow(Base):
    """Nightly-recomputed per-strategy/per-regime aggregates (spec §8/§9).
    Feed synthesizer confidence + selector priors; risk limits are NEVER
    auto-tuned from these."""

    __tablename__ = "strategy_stats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    regime: Mapped[str] = mapped_column(String(24), default="*", index=True)  # "*" = all
    resolved_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    hit_rate: Mapped[float] = mapped_column(Float, default=0.0)
    expectancy_r: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow)
    __table_args__ = (UniqueConstraint("strategy", "regime", name="uq_strategy_regime"),)


# --------------------------------------------------------------------------
# Phase 5 — web app
# --------------------------------------------------------------------------
class ChatMessageRow(Base):
    """Chat transcript (spec §8 chat_messages). Tool calls the assistant made
    are recorded for auditability; chat can never bypass the risk engine."""

    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    role: Mapped[str] = mapped_column(String(12))  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str | None] = mapped_column(String(48), nullable=True)


class AppSettingRow(Base):
    """Key-value app settings editable in the UI (watchlist, quiet hours,
    onboarding flag). Bootstrap secrets stay in .env; provider keys live in
    provider_credentials."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=UTCNow, onupdate=UTCNow
    )


class ApiUsage(Base):
    """Cost meter: one row per external call (LLM or data API)."""

    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=UTCNow, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    endpoint: Mapped[str] = mapped_column(String(128), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str] = mapped_column(Text, default="")
