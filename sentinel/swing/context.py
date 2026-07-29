"""Lightweight market context for the swing screen.

The swing universe is the whole static universe, but the screen + technicals
need only daily bars, the stored fundamentals snapshot, and VIX/macro for the
regime. Building the FULL context (news, earnings) for every name on each scan
would be wasteful, so this loads bars + macro + a single bulk fundamentals
query. The full context is built later for the handful of symbols that survive
the screen (see pipeline.run_swing_scan), where the review actually needs news
and earnings dates.

Fundamentals are included because the screener's quality filters (market cap
floor, known sector) are part of the swing bar too — and reading a table that
is already populated costs one query, not one call.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.data.context import MarketContext, SymbolContext, _load_bars
from sentinel.db.models import FundamentalsRow, MacroSeriesRow
from sentinel.providers.types import MacroPoint


def _load_macro(db: Session) -> dict[str, list[MacroPoint]]:
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
    return macro


def _load_fundamentals(db: Session, symbols: list[str]) -> dict[str, FundamentalsRow]:
    """One bulk query instead of a per-symbol db.get in the screen loop."""
    if not symbols:
        return {}
    rows = db.execute(
        select(FundamentalsRow).where(FundamentalsRow.symbol.in_(symbols))
    ).scalars().all()
    return {row.symbol: row for row in rows}


def build_screen_context(
    db: Session, symbols: list[str], lookback_days: int = 400
) -> MarketContext:
    """Bars + fundamentals + macro (no per-symbol news/earnings queries)."""
    now = datetime.now(UTC)
    fundamentals = _load_fundamentals(db, symbols)
    contexts = {}
    for symbol in symbols:
        fam = fundamentals.get(symbol)
        contexts[symbol] = SymbolContext(
            symbol=symbol,
            daily_bars=_load_bars(db, symbol, lookback_days),
            news=[],
            sector=fam.sector if fam else "",
            market_cap=fam.market_cap if fam else None,
            pe=fam.pe if fam else None,
            ps=fam.ps if fam else None,
            beta=fam.beta if fam else None,
            week52_high=fam.week52_high if fam else None,
            week52_low=fam.week52_low if fam else None,
        )
    return MarketContext(
        as_of=now,
        spy_bars=_load_bars(db, "SPY", lookback_days),
        macro=_load_macro(db),
        symbols=contexts,
    )
