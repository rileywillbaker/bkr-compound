"""Typed domain objects crossing provider boundaries.

Every external payload is normalized into one of these pydantic models at the
provider edge; nothing downstream ever touches raw provider JSON.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class Bar(BaseModel):
    symbol: str
    ts: datetime  # UTC, bar open time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timeframe: Literal["1Min", "5Min", "15Min", "1Hour", "1Day"] = "1Day"


class Quote(BaseModel):
    symbol: str
    ts: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None


class NewsItem(BaseModel):
    provider_id: str  # provider-native id for dedup
    symbol: str | None = None  # None for market-wide news
    headline: str
    summary: str = ""
    source: str = ""
    url: str = ""
    published_at: datetime


class CompanyProfile(BaseModel):
    symbol: str
    name: str = ""
    sector: str = ""  # Finnhub "finnhubIndustry"
    market_cap: float | None = None  # millions USD
    exchange: str = ""


class BasicFinancials(BaseModel):
    symbol: str
    pe: float | None = None
    ps: float | None = None
    eps_growth_ttm: float | None = None
    revenue_growth_ttm: float | None = None
    beta: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None


class EarningsEvent(BaseModel):
    symbol: str
    date: date
    hour: str = ""  # bmo / amc / dmh / ""
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None


class RecommendationTrend(BaseModel):
    symbol: str
    period: date
    strong_buy: int = 0
    buy: int = 0
    hold: int = 0
    sell: int = 0
    strong_sell: int = 0


class InsiderTransaction(BaseModel):
    symbol: str
    name: str = ""
    share_change: int = 0  # positive = acquired
    transaction_date: date
    transaction_price: float | None = None
    filing_date: date | None = None


class MacroPoint(BaseModel):
    series_id: str
    date: date
    value: float | None = None  # FRED uses "." for missing


class Filing(BaseModel):
    symbol: str
    cik: str
    form: str  # 8-K, 10-Q, 10-K, 4
    filed_at: date
    accession_no: str
    url: str = ""
    description: str = ""


class ProviderCheck(BaseModel):
    """Result of a credential/connectivity validation ("Test connection")."""

    provider: str
    ok: bool
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.utcnow())


class StockOverview(BaseModel):
    """Supplemental per-symbol snapshot (Yahoo). Used to fill gaps when the
    primary research provider's fields are premium-gated or missing —
    never to override a value the primary provider already supplied."""

    symbol: str
    price: float | None = None
    pe: float | None = None
    forward_pe: float | None = None
    market_cap: float | None = None  # millions USD, normalized to match BasicFinancials
    week52_high: float | None = None
    week52_low: float | None = None
    analyst_target_mean: float | None = None
    analyst_recommendation: str | None = None  # e.g. "buy", "hold" (Yahoo's own label)
    sector: str = ""
    short_pct_float: float | None = None


class ScreenerRow(BaseModel):
    """One matched row from an external screener (Finviz). Fields beyond
    symbol/price are whatever columns the provider's default export included
    — kept as a raw string dict since the exact column set isn't guaranteed."""

    symbol: str
    price: float | None = None
    raw: dict[str, str] = Field(default_factory=dict)


class ShortInterestSnapshot(BaseModel):
    """Short-interest / dark-pool snapshot (Fintel). Field availability
    depends on Fintel's plan tier — analysts should treat any None as
    'unavailable', never as zero."""

    symbol: str
    as_of: date
    short_percent_float: float | None = None
    short_percent_shares_outstanding: float | None = None
    days_to_cover: float | None = None
    short_interest_change_pct: float | None = None
    dark_pool_short_volume_pct: float | None = None  # % of volume off-exchange/dark-pool
