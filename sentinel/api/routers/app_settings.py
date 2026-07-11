"""App settings API (spec §7.7): watchlist manager, starting equity, alert
quiet hours, onboarding completion. Risk profile has its own versioned
endpoint (/api/risk/profile); provider keys live in /api/providers."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.db.base import get_db
from sentinel.db.settings_store import (
    ONBOARDED_KEY,
    QUIET_HOURS_KEY,
    STARTING_EQUITY_KEY,
    get_setting,
    get_starting_equity,
    get_watchlist,
    is_onboarded,
    set_setting,
    set_watchlist,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.get("")
def get_all(db: Session = Depends(get_db)) -> dict:
    from sentinel.data.universe import load_static_universe

    return {
        # highlighted tickers only — the scan universe is the static list
        "watchlist": get_watchlist(db),
        "universe_size": len(load_static_universe()),
        "starting_equity": get_starting_equity(db),
        "alert_quiet_hours": get_setting(db, QUIET_HOURS_KEY),
        "onboarding_complete": is_onboarded(db),
    }


class WatchlistIn(BaseModel):
    """Highlighted tickers (always scanned + surfaced); never a universe cap."""

    symbols: list[str] = Field(min_length=1, max_length=100)


@router.put("/watchlist")
def put_watchlist(body: WatchlistIn, db: Session = Depends(get_db)) -> dict:
    cleaned = [s.strip().upper() for s in body.symbols if s.strip()]
    bad = [s for s in cleaned if not _TICKER_RE.match(s)]
    if bad:
        raise HTTPException(422, f"invalid ticker symbols: {', '.join(bad[:5])}")
    saved = set_watchlist(db, cleaned)
    db.commit()
    return {"watchlist": saved}


class EquityIn(BaseModel):
    starting_equity: float = Field(gt=0, le=1_000_000_000)


@router.put("/equity")
def put_equity(body: EquityIn, db: Session = Depends(get_db)) -> dict:
    set_setting(db, STARTING_EQUITY_KEY, body.starting_equity)
    db.commit()
    return {"starting_equity": body.starting_equity}


class QuietHoursIn(BaseModel):
    start: str | None = None  # "HH:MM" ET; both None clears quiet hours
    end: str | None = None


@router.put("/quiet-hours")
def put_quiet_hours(body: QuietHoursIn, db: Session = Depends(get_db)) -> dict:
    if (body.start is None) != (body.end is None):
        raise HTTPException(422, "provide both start and end, or neither")
    value = None
    if body.start and body.end:
        if not (_HHMM_RE.match(body.start) and _HHMM_RE.match(body.end)):
            raise HTTPException(422, "quiet hours must be HH:MM 24h format")
        value = {"start": body.start, "end": body.end}
    set_setting(db, QUIET_HOURS_KEY, value)
    db.commit()
    return {"alert_quiet_hours": value}


@router.post("/onboarding-complete")
def complete_onboarding(db: Session = Depends(get_db)) -> dict:
    set_setting(db, ONBOARDED_KEY, True)
    db.commit()
    return {"onboarding_complete": True}
