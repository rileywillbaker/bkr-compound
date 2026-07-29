"""Swing Trading tab backend (book="swing").

Informational only — like everything in B-Quant, nothing here executes a
trade. Setups are surfaced for the user's own decision and every one has
already passed the deterministic risk engine. The long-term ("core") signals
feed lives under /api/signals and is unaffected.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.api.routers.signals import _signal_dict
from sentinel.db.base import get_db
from sentinel.pipeline.persist import list_signals
from sentinel.swing.pipeline import SwingScanResult, last_swing_run, run_swing_scan

router = APIRouter(prefix="/api/trading", tags=["trading"])


class SwingFeed(BaseModel):
    regime: str | None
    generated_at: datetime | None
    universe_size: int | None
    scanned: int | None
    screened_count: int | None
    alerts_sent: int | None
    signals: list[dict]
    disclaimer: str = DISCLAIMER


class SwingScanRequest(BaseModel):
    # Manual "Scan now": defaults to the full S&P 500 swing universe; any
    # explicit ticker list may be passed. Manual runs never send alerts.
    symbols: list[str] | None = None
    use_llm: bool = True


@router.get("")
def swing_feed(
    limit: int = Query(default=50, le=200), db: Session = Depends(get_db)
) -> SwingFeed:
    """Latest swing setups plus the last scan's summary for the banner."""
    rows = list_signals(db, book="swing", limit=limit)
    last = last_swing_run()
    return SwingFeed(
        regime=last.regime if last else None,
        generated_at=last.generated_at if last else None,
        universe_size=last.universe_size if last else None,
        scanned=last.scanned if last else None,
        screened_count=len(last.screened) if last else None,
        alerts_sent=last.alerts_sent if last else None,
        signals=[_signal_dict(r) for r in rows],
    )


@router.post("/scan")
def swing_scan(
    request: SwingScanRequest, db: Session = Depends(get_db)
) -> SwingScanResult:
    """Run the swing pipeline on demand. No alerts (manual runs never notify);
    scheduled scans are what send Telegram swing alerts."""
    result = run_swing_scan(
        db, symbols=request.symbols, use_llm=request.use_llm, send_alerts=False
    )
    db.commit()
    return result
