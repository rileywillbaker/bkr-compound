"""App settings API (spec §7.7): watchlist manager, starting equity, alert
quiet hours, onboarding completion. Risk profile has its own versioned
endpoint (/api/risk/profile); provider keys live in /api/providers."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.db.base import get_db
from sentinel.db.settings_store import (
    FOCUS_SET_SIZE_KEY,
    FULL_UNIVERSE_DEEP_INGEST_KEY,
    ONBOARDED_KEY,
    QUIET_HOURS_KEY,
    STARTING_EQUITY_KEY,
    focus_set_size,
    full_universe_deep_ingest,
    get_setting,
    get_starting_equity,
    get_watchlist,
    is_onboarded,
    set_setting,
    set_watchlist,
)
from sentinel.modes import all_policies, get_mode, get_policy, set_mode

router = APIRouter(prefix="/api/settings", tags=["settings"])

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.get("")
def get_all(db: Session = Depends(get_db)) -> dict:
    from sentinel.data.universe import load_static_universe, universe_files

    policy = get_policy(db)
    return {
        # highlighted tickers only — the scan universe is the static list
        "watchlist": get_watchlist(db),
        "universe_size": len(load_static_universe()),
        "universe_files": [p.name for p in universe_files()],
        "starting_equity": get_starting_equity(db),
        "alert_quiet_hours": get_setting(db, QUIET_HOURS_KEY),
        "onboarding_complete": is_onboarded(db),
        "operating_mode": policy.mode,
        "operating_mode_label": policy.label,
        "full_universe_deep_ingest": full_universe_deep_ingest(db),
        "focus_set_size": focus_set_size(db),
    }


@router.get("/modes")
def list_modes(db: Session = Depends(get_db)) -> dict:
    """Every operating mode plus the one in force — the cost control surface."""
    return {
        "current": get_mode(db),
        "modes": [
            {
                "mode": p.mode,
                "label": p.label,
                "description": p.description,
                "scan_depth": p.scan_depth,
                "on_demand_depth": p.on_demand_depth,
                "max_llm_candidates_per_scan": p.max_llm_candidates_per_scan,
            }
            for p in all_policies()
        ],
    }


class ModeIn(BaseModel):
    mode: str


@router.put("/mode")
def put_mode(body: ModeIn, db: Session = Depends(get_db)) -> dict:
    """Switch operating mode. Takes effect on the next scan; nothing about the
    risk engine or position sizing changes with it."""
    try:
        saved = set_mode(db, body.mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    policy = get_policy(db)
    return {"operating_mode": saved, "label": policy.label, "description": policy.description}


class IngestScopeIn(BaseModel):
    full_universe_deep_ingest: bool | None = None
    focus_set_size: int | None = Field(default=None, ge=10, le=500)


@router.put("/ingest-scope")
def put_ingest_scope(body: IngestScopeIn, db: Session = Depends(get_db)) -> dict:
    """How wide the expensive per-symbol data pulls go each morning."""
    if body.full_universe_deep_ingest is not None:
        set_setting(db, FULL_UNIVERSE_DEEP_INGEST_KEY, body.full_universe_deep_ingest)
    if body.focus_set_size is not None:
        set_setting(db, FOCUS_SET_SIZE_KEY, body.focus_set_size)
    db.commit()
    return {
        "full_universe_deep_ingest": full_universe_deep_ingest(db),
        "focus_set_size": focus_set_size(db),
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
