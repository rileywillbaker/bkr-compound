"""Data-staleness watchdog (spec §4.9).

The three-scans-per-day cadence keeps DAILY bars fresh (08:30 full ingest,
15:30 partial refresh, 16:45 post-close); nothing ingests intraday bars
anymore, so intraday age is meaningless — checking it made the watchdog
warn forever once the old rolling scan was retired. Stale now means:
during market hours the newest 1Day bar predates the previous trading
session, i.e. the morning ingest hasn't succeeded for at least a full
session (the failure mode that actually bites: worker dead, host asleep
through every grace window, provider outage).

The watchdog is dashboard-only: it records a WARN system_event (at most
one per ALERT_COOLDOWN while the condition persists — it ticks every 5
minutes and must not log 78 times a day about one problem) and never
sends Telegram. Per user decision 2026-07-17, Telegram is reserved for
scan results; operational problems surface in the UI event log.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.data.market_hours import ET, is_market_open, previous_trading_day
from sentinel.db.models import BarRow, SystemEvent

ALERT_COOLDOWN = timedelta(hours=6)


def check_staleness(db: Session, now: datetime | None = None) -> bool:
    """Returns True if data is fresh (or market closed); False if stale."""
    now = now or datetime.now(UTC)
    if not is_market_open(now):
        return True
    newest = db.execute(
        select(func.max(BarRow.ts)).where(BarRow.timeframe == "1Day")
    ).scalar_one_or_none()
    if newest is None:
        detail = "no daily bars ingested yet"
    else:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=UTC)
        newest_day = newest.astimezone(ET).date()
        if newest_day >= previous_trading_day(now):
            return True
        age_days = (now.astimezone(ET).date() - newest_day).days
        detail = f"newest daily bar is {age_days} days old ({newest_day})"

    last_warned = db.execute(
        select(func.max(SystemEvent.ts)).where(SystemEvent.kind == "watchdog.stale_data")
    ).scalar_one_or_none()
    if last_warned is not None:
        if last_warned.tzinfo is None:
            last_warned = last_warned.replace(tzinfo=UTC)
        if now - last_warned < ALERT_COOLDOWN:
            return False  # still stale, but already recorded this episode

    db.add(
        SystemEvent(
            level="WARN",
            kind="watchdog.stale_data",
            message=f"Market data stale during market hours: {detail}",
        )
    )
    db.flush()
    return False
