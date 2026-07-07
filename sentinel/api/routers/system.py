"""System view endpoints: event log, ingestion trigger, cost meter."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.data import ingest
from sentinel.data.market_hours import is_market_open, market_schedule
from sentinel.db.base import get_db
from sentinel.db.models import ApiUsage, BarRow, SystemEvent

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/events")
def events(
    limit: int = 100, level: str | None = None, db: Session = Depends(get_db)
) -> list[dict]:
    q = select(SystemEvent).order_by(SystemEvent.ts.desc()).limit(min(limit, 500))
    if level:
        q = q.where(SystemEvent.level == level.upper())
    rows = db.execute(q).scalars().all()
    return [
        {
            "ts": r.ts,
            "level": r.level,
            "kind": r.kind,
            "message": r.message,
            "payload": r.payload,
        }
        for r in rows
    ]


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    newest_daily = db.execute(
        select(func.max(BarRow.ts)).where(BarRow.timeframe == "1Day")
    ).scalar_one_or_none()
    bar_count = db.execute(select(func.count()).select_from(BarRow)).scalar_one()
    window = market_schedule()
    return {
        "market_open": is_market_open(),
        "todays_session_utc": [w.isoformat() for w in window] if window else None,
        "bars_stored": bar_count,
        "newest_daily_bar": newest_daily,
    }


@router.get("/costs")
def costs(days: int = 7, db: Session = Depends(get_db)) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(
        select(
            func.date_trunc("day", ApiUsage.ts).label("day"),
            ApiUsage.provider,
            func.count().label("calls"),
            func.sum(ApiUsage.tokens_in).label("tokens_in"),
            func.sum(ApiUsage.tokens_out).label("tokens_out"),
            func.sum(ApiUsage.cost_usd).label("cost_usd"),
        )
        .where(ApiUsage.ts >= cutoff)
        .group_by("day", ApiUsage.provider)
        .order_by("day")
    ).all()
    return [
        {
            "day": r.day,
            "provider": r.provider,
            "calls": r.calls,
            "tokens_in": int(r.tokens_in or 0),
            "tokens_out": int(r.tokens_out or 0),
            "cost_usd": float(r.cost_usd or 0),
        }
        for r in rows
    ]


@router.post("/ingest")
def trigger_ingest(db: Session = Depends(get_db)) -> dict:
    """Manual full ingest (used right after onboarding)."""
    return {
        "bars": ingest.ingest_bars(db),
        "quotes": ingest.ingest_quotes(db),
        "news": ingest.ingest_news(db),
        "fundamentals": ingest.ingest_fundamentals(db),
        "earnings": ingest.ingest_earnings_calendar(db),
        "macro": ingest.ingest_macro(db),
        "filings": ingest.ingest_filings(db),
    }
