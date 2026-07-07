"""Data-staleness watchdog (spec §4.9).

During market hours, if the newest intraday bar is older than the threshold,
record a WARN system_event and notify via the alert channel (wired in
Phase 4; before that, the event row is the signal)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.data.market_hours import is_market_open
from sentinel.db.models import BarRow, SystemEvent

STALE_AFTER = timedelta(minutes=15)


def check_staleness(db: Session, now: datetime | None = None) -> bool:
    """Returns True if data is fresh (or market closed); False if stale."""
    now = now or datetime.now(UTC)
    if not is_market_open(now):
        return True
    newest = db.execute(
        select(func.max(BarRow.ts)).where(BarRow.timeframe != "1Day")
    ).scalar_one_or_none()
    if newest is None:
        detail = "no intraday bars ingested yet"
    else:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=UTC)
        age = now - newest
        if age <= STALE_AFTER:
            return True
        detail = f"newest intraday bar is {age.total_seconds() / 60:.0f} min old"

    db.add(
        SystemEvent(
            level="WARN",
            kind="watchdog.stale_data",
            message=f"Market data stale during market hours: {detail}",
        )
    )
    db.flush()
    try:  # Phase 4 wires a real alert channel; ignore if unavailable
        from sentinel.alerts.router import send_ops_alert

        send_ops_alert(db, f"⚠️ B-Quant data stale: {detail}")
    except Exception:
        pass
    return False
