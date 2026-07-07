"""Manual portfolio entry + live valuation (spec §7.3).

Trades are always user-entered; positions are derived from trades.
"""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.base import get_db
from sentinel.db.models import FundamentalsRow, Position, TradeRow
from sentinel.portfolio.state import build_portfolio_state, cash_balance

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeIn(BaseModel):
    symbol: str
    side: str = Field(pattern="^(BUY|SELL)$")
    shares: int = Field(gt=0)
    price: float = Field(gt=0)
    signal_id: str | None = None
    note: str = ""


def _apply_trade(db: Session, trade: TradeIn) -> Position:
    symbol = trade.symbol.upper()
    pos = db.get(Position, symbol)
    if pos is None:
        pos = Position(symbol=symbol, shares=0, cost_basis=Decimal(0))
        db.add(pos)
    if trade.side == "BUY":
        total_cost = float(pos.cost_basis) * pos.shares + trade.price * trade.shares
        pos.shares += trade.shares
        pos.cost_basis = Decimal(str(round(total_cost / pos.shares, 6)))
    else:
        if trade.shares > pos.shares:
            raise HTTPException(422, f"cannot sell {trade.shares}, only {pos.shares} held")
        pos.shares -= trade.shares
        if pos.shares == 0:
            pos.cost_basis = Decimal(0)
    pos.updated_at = datetime.now(UTC)
    db.add(
        TradeRow(
            symbol=symbol,
            side=trade.side,
            shares=trade.shares,
            price=Decimal(str(trade.price)),
            signal_id=trade.signal_id,
            note=trade.note,
        )
    )
    db.flush()
    return pos


@router.post("/trades")
def record_trade(trade: TradeIn, db: Session = Depends(get_db)) -> dict:
    pos = _apply_trade(db, trade)
    return {"symbol": pos.symbol, "shares": pos.shares, "cost_basis": float(pos.cost_basis)}


@router.get("/trades")
def list_trades(limit: int = 200, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(TradeRow).order_by(TradeRow.ts.desc()).limit(min(limit, 1000))
    ).scalars().all()
    return [
        {
            "id": t.id,
            "ts": t.ts,
            "symbol": t.symbol,
            "side": t.side,
            "shares": t.shares,
            "price": float(t.price),
            "signal_id": t.signal_id,
            "note": t.note,
        }
        for t in rows
    ]


@router.get("")
def valuation(db: Session = Depends(get_db)) -> dict:
    state = build_portfolio_state(db)
    positions: list[dict] = []
    for p in state.positions:
        row = db.get(Position, p.symbol)
        fam = db.get(FundamentalsRow, p.symbol)
        cost = float(row.cost_basis) if row else 0.0
        positions.append(
            {
                "symbol": p.symbol,
                "shares": p.shares,
                "cost_basis": cost,
                "mark": p.price,
                "market_value": round(p.market_value, 2),
                "unrealized_pnl": round((p.price - cost) * p.shares, 2),
                "sector": fam.sector if fam else "",
                "weight_pct": round(p.market_value / state.equity * 100, 2)
                if state.equity
                else 0.0,
            }
        )
    sector_weights: dict[str, float] = {}
    for entry in positions:
        key = entry["sector"] or "(unknown)"
        sector_weights[key] = round(sector_weights.get(key, 0.0) + entry["weight_pct"], 2)
    return {
        "equity": round(state.equity, 2),
        "cash": round(cash_balance(db), 2),
        "high_water_mark": round(state.high_water_mark, 2),
        "day_pnl": round(state.day_pnl, 2),
        "gross_exposure_pct": round(state.gross_exposure / state.equity * 100, 2)
        if state.equity
        else 0.0,
        "positions": positions,
        "sector_weights": sector_weights,
    }
