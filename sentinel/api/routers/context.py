"""Market-context endpoint — the Phase 1 deliverable: a dashboard-less API
returning live market context."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from sentinel.config import get_settings
from sentinel.data.context import MarketContext, build_market_context
from sentinel.db.base import get_db

router = APIRouter(prefix="/api/context", tags=["context"])


@router.get("")
def market_context(
    symbols: str | None = None, db: Session = Depends(get_db)
) -> MarketContext:
    syms = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else get_settings().watchlist_symbols
    )
    return build_market_context(db, syms)
