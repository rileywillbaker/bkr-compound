"""MarketContext: the typed snapshot handed to the agent pipeline (spec §3).

Assembled purely from the database — the pipeline never calls providers
directly, which keeps runs reproducible and testable."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FundamentalsRow,
    MacroSeriesRow,
    NewsItemRow,
)
from sentinel.providers.types import Bar, EarningsEvent, MacroPoint, NewsItem


class SymbolContext(BaseModel):
    symbol: str
    daily_bars: list[Bar]  # ascending by ts
    news: list[NewsItem]
    sector: str = ""
    market_cap: float | None = None
    pe: float | None = None
    ps: float | None = None
    beta: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    next_earnings: EarningsEvent | None = None


class MarketContext(BaseModel):
    as_of: datetime
    spy_bars: list[Bar]
    macro: dict[str, list[MacroPoint]]  # series_id -> points ascending
    symbols: dict[str, SymbolContext]
    data_fresh: bool = True


def _load_bars(db: Session, symbol: str, days: int) -> list[Bar]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        db.execute(
            select(BarRow)
            .where(BarRow.symbol == symbol, BarRow.timeframe == "1Day", BarRow.ts >= cutoff)
            .order_by(BarRow.ts)
        )
        .scalars()
        .all()
    )
    return [
        Bar(
            symbol=r.symbol,
            ts=r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=UTC),
            open=Decimal(r.open),
            high=Decimal(r.high),
            low=Decimal(r.low),
            close=Decimal(r.close),
            volume=r.volume,
            timeframe="1Day",
        )
        for r in rows
    ]


def build_market_context(
    db: Session, symbols: list[str], lookback_days: int = 400
) -> MarketContext:
    now = datetime.now(UTC)
    news_cutoff = now - timedelta(days=5)

    macro: dict[str, list[MacroPoint]] = {}
    for (series_id,) in db.execute(select(MacroSeriesRow.series_id).distinct()).all():
        rows = (
            db.execute(
                select(MacroSeriesRow)
                .where(MacroSeriesRow.series_id == series_id)
                .order_by(MacroSeriesRow.date)
            )
            .scalars()
            .all()
        )
        macro[series_id] = [
            MacroPoint(series_id=r.series_id, date=r.date, value=r.value) for r in rows
        ]

    contexts: dict[str, SymbolContext] = {}
    for symbol in symbols:
        fam = db.get(FundamentalsRow, symbol)
        news_rows = (
            db.execute(
                select(NewsItemRow)
                .where(NewsItemRow.symbol == symbol, NewsItemRow.published_at >= news_cutoff)
                .order_by(NewsItemRow.published_at.desc())
                .limit(50)
            )
            .scalars()
            .all()
        )
        earnings_row = (
            db.execute(
                select(EarningsCalendarRow)
                .where(
                    EarningsCalendarRow.symbol == symbol,
                    EarningsCalendarRow.date >= date.today(),
                )
                .order_by(EarningsCalendarRow.date)
                .limit(1)
            )
            .scalars()
            .first()
        )
        contexts[symbol] = SymbolContext(
            symbol=symbol,
            daily_bars=_load_bars(db, symbol, lookback_days),
            news=[
                NewsItem(
                    provider_id=n.provider_id,
                    symbol=n.symbol,
                    headline=n.headline,
                    summary=n.summary,
                    source=n.source,
                    url=n.url,
                    published_at=n.published_at
                    if n.published_at.tzinfo
                    else n.published_at.replace(tzinfo=UTC),
                )
                for n in news_rows
            ],
            sector=fam.sector if fam else "",
            market_cap=fam.market_cap if fam else None,
            pe=fam.pe if fam else None,
            ps=fam.ps if fam else None,
            beta=fam.beta if fam else None,
            week52_high=fam.week52_high if fam else None,
            week52_low=fam.week52_low if fam else None,
            next_earnings=EarningsEvent(
                symbol=earnings_row.symbol,
                date=earnings_row.date,
                hour=earnings_row.hour,
                eps_estimate=earnings_row.eps_estimate,
                eps_actual=earnings_row.eps_actual,
                revenue_estimate=earnings_row.revenue_estimate,
                revenue_actual=earnings_row.revenue_actual,
            )
            if earnings_row
            else None,
        )

    return MarketContext(
        as_of=now,
        spy_bars=_load_bars(db, "SPY", lookback_days),
        macro=macro,
        symbols=contexts,
    )
