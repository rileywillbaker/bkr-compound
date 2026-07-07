"""Signal history + user decision capture (spec §7.4/§7.5 backend).

Decisions feed the trade journal automatically; there is no endpoint that
executes anything — signals are information for the user's own action.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.db.base import get_db
from sentinel.db.models import JournalEntryRow, SignalRow
from sentinel.pipeline.persist import get_risk_check, get_signal, list_signals, record_decision

router = APIRouter(prefix="/api/signals", tags=["signals"])


def _signal_dict(row: SignalRow) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at,
        "run_id": row.run_id,
        "ticker": row.ticker,
        "action": row.action,
        "shares": row.shares,
        "max_entry_price": float(row.max_entry_price) if row.max_entry_price else None,
        "stop_loss": float(row.stop_loss) if row.stop_loss else None,
        "take_profit": float(row.take_profit) if row.take_profit else None,
        "confidence": row.confidence,
        "expected_return_pct": row.expected_return_pct,
        "risk_score": row.risk_score,
        "time_horizon": row.time_horizon,
        "strategy": row.strategy,
        "regime": row.regime,
        "explanation": row.explanation,
        "deterministic_only": row.deterministic_only,
        "alert_sent": row.alert_sent,
        "user_decision": row.user_decision,
        "decided_at": row.decided_at,
    }


@router.get("")
def signals(
    ticker: str | None = None,
    action: str | None = None,
    decision: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
) -> dict:
    rows = list_signals(
        db, ticker=ticker, action=action, decision=decision, since=since, limit=limit
    )
    return {"signals": [_signal_dict(r) for r in rows], "disclaimer": DISCLAIMER}


@router.get("/{signal_id}")
def signal_detail(signal_id: str, db: Session = Depends(get_db)) -> dict:
    row = get_signal(db, signal_id)
    if row is None:
        raise HTTPException(404, "signal not found")
    check = get_risk_check(db, signal_id)
    return {
        **_signal_dict(row),
        "evidence": row.evidence or [],
        "risk_check": {
            "approved": check.approved,
            "profile_version": check.profile_version,
            "checked_at": check.checked_at,
            "rules": check.rules,
        }
        if check
        else None,
        "disclaimer": DISCLAIMER,
    }


class DecisionIn(BaseModel):
    decision: Literal["taken", "skipped", "modified"]
    note: str = Field(default="", max_length=2000)


@router.post("/{signal_id}/decision")
def decide(signal_id: str, body: DecisionIn, db: Session = Depends(get_db)) -> dict:
    row = record_decision(db, signal_id, body.decision, note=body.note)
    if row is None:
        raise HTTPException(404, "signal not found")
    db.commit()
    return _signal_dict(row)


@router.get("/journal/entries")
def journal(limit: int = Query(default=100, le=500), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(JournalEntryRow).order_by(JournalEntryRow.ts.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "ts": r.ts,
            "signal_id": r.signal_id,
            "ticker": r.ticker,
            "decision": r.decision,
            "note": r.note,
        }
        for r in rows
    ]
