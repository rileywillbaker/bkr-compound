from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from sentinel.data import watchdog
from sentinel.db.models import BarRow, SystemEvent


def _bar(ts: datetime, timeframe: str = "15Min") -> BarRow:
    return BarRow(
        symbol="SPY",
        timeframe=timeframe,
        ts=ts,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )


def _events(db) -> list[SystemEvent]:
    return list(db.execute(select(SystemEvent)).scalars())


def test_fresh_when_market_closed(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: False)
    assert watchdog.check_staleness(db) is True
    assert _events(db) == []


def test_stale_when_no_bars(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    assert watchdog.check_staleness(db) is False
    events = _events(db)
    assert events and events[0].kind == "watchdog.stale_data"


def test_fresh_with_recent_bar(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    db.add(_bar(now - timedelta(minutes=5)))
    db.flush()
    assert watchdog.check_staleness(db, now=now) is True


def test_stale_with_old_bar(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    db.add(_bar(now - timedelta(minutes=45)))
    db.flush()
    assert watchdog.check_staleness(db, now=now) is False


def test_daily_bars_do_not_count_as_intraday(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    db.add(_bar(now, timeframe="1Day"))
    db.flush()
    assert watchdog.check_staleness(db, now=now) is False
