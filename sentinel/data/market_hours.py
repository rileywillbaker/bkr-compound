"""NYSE market-hours helpers (exchange holiday calendar via
pandas_market_calendars). All scheduling decisions go through here."""

from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")


@lru_cache
def _nyse():
    return mcal.get_calendar("NYSE")


def market_schedule(day: datetime | None = None) -> tuple[datetime, datetime] | None:
    """(open, close) in UTC for the given ET day, or None if closed."""
    ref = (day or datetime.now(UTC)).astimezone(ET).date()
    sched = _nyse().schedule(start_date=ref, end_date=ref)
    if sched.empty:
        return None
    row = sched.iloc[0]
    return (
        row["market_open"].to_pydatetime().astimezone(UTC),
        row["market_close"].to_pydatetime().astimezone(UTC),
    )


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    window = market_schedule(now)
    if window is None:
        return False
    open_, close = window
    return open_ <= now <= close


def is_trading_day(day: datetime | None = None) -> bool:
    return market_schedule(day) is not None


def trading_days_until(target: pd.Timestamp | datetime, now: datetime | None = None) -> int:
    """Number of trading days from today (exclusive) to target (inclusive)."""
    today = (now or datetime.now(UTC)).astimezone(ET).date()
    target_date = pd.Timestamp(target).date()
    if target_date <= today:
        return 0
    days = _nyse().valid_days(start_date=today, end_date=target_date)
    return max(0, len([d for d in days if d.date() > today]))
