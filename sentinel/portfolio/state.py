"""Assemble the PortfolioState snapshot the risk engine consumes.

This module does the I/O (DB reads) so the engine can stay pure.
Marks come from quotes_latest, falling back to the newest daily close, then
to cost basis.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.config import get_settings
from sentinel.db.models import BarRow, EquitySnapshot, FundamentalsRow, Position, QuoteLatest
from sentinel.risk.engine import PortfolioState, PositionState


def _mark_price(db: Session, symbol: str, fallback: float) -> float:
    quote = db.get(QuoteLatest, symbol)
    if quote is not None and quote.last:
        return float(quote.last)
    if quote is not None and quote.bid and quote.ask:
        return (float(quote.bid) + float(quote.ask)) / 2
    close = db.execute(
        select(BarRow.close)
        .where(BarRow.symbol == symbol, BarRow.timeframe == "1Day")
        .order_by(BarRow.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    if close is not None:
        return float(close)
    return fallback


def cash_balance(db: Session) -> float:
    """Cash = starting equity + realized proceeds - purchases (from trades)."""
    from sentinel.db.models import TradeRow

    cash = get_settings().starting_equity
    for trade in db.execute(select(TradeRow)).scalars():
        signed = -1 if trade.side == "BUY" else 1
        cash += signed * trade.shares * float(trade.price)
    return cash


def build_portfolio_state(db: Session, now: datetime | None = None) -> PortfolioState:
    now = now or datetime.now(UTC)
    positions: list[PositionState] = []
    for pos in db.execute(select(Position).where(Position.shares != 0)).scalars():
        fam = db.get(FundamentalsRow, pos.symbol)
        positions.append(
            PositionState(
                symbol=pos.symbol,
                shares=pos.shares,
                price=_mark_price(db, pos.symbol, float(pos.cost_basis)),
                sector=fam.sector if fam else "",
            )
        )
    equity = cash_balance(db) + sum(p.market_value for p in positions)

    hwm = db.execute(select(func.max(EquitySnapshot.equity))).scalar_one_or_none()
    high_water_mark = max(float(hwm or 0.0), equity)

    # Day P&L = equity now vs last snapshot before today (prior close).
    today_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    prior = db.execute(
        select(EquitySnapshot.equity)
        .where(EquitySnapshot.ts < today_start)
        .order_by(EquitySnapshot.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    day_pnl = equity - float(prior) if prior is not None else 0.0

    return PortfolioState(
        equity=equity,
        high_water_mark=high_water_mark,
        day_pnl=day_pnl,
        positions=positions,
    )


def snapshot_equity(db: Session) -> float:
    state = build_portfolio_state(db)
    db.add(EquitySnapshot(equity=state.equity))
    db.flush()
    return state.equity


def compute_correlations(
    db: Session, candidate: str, held_symbols: list[str], days: int = 90
) -> dict[str, float]:
    """Pearson correlation of daily returns over `days` calendar days.

    Symbols with insufficient overlapping history are omitted — the risk
    engine treats missing correlations as a failure (conservative).
    """
    if not held_symbols:
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=days)
    symbols = [candidate, *held_symbols]
    rows = db.execute(
        select(BarRow.symbol, BarRow.ts, BarRow.close).where(
            BarRow.symbol.in_(symbols),
            BarRow.timeframe == "1Day",
            BarRow.ts >= cutoff,
        )
    ).all()
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["symbol", "ts", "close"])
    df["close"] = df["close"].astype(float)
    wide = df.pivot_table(index="ts", columns="symbol", values="close").sort_index()
    returns = wide.pct_change().dropna(how="all")
    if candidate not in returns.columns:
        return {}
    out: dict[str, float] = {}
    for sym in held_symbols:
        if sym == candidate:
            out[sym] = 1.0
            continue
        if sym not in returns.columns:
            continue
        pair = returns[[candidate, sym]].dropna()
        if len(pair) < 20:  # need meaningful overlap
            continue
        corr = float(pair[candidate].corr(pair[sym]))
        if not np.isnan(corr):
            out[sym] = corr
    return out
