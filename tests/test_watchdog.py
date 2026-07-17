from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from sentinel.data import watchdog
from sentinel.db.models import BarRow, SystemEvent


def _bar(ts: datetime, timeframe: str = "1Day") -> BarRow:
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


def test_fresh_with_recent_daily_bar(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    db.add(_bar(now))  # today's (partial) daily bar
    db.flush()
    assert watchdog.check_staleness(db, now=now) is True
    assert _events(db) == []


def test_stale_with_old_daily_bar(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    db.add(_bar(now - timedelta(days=9)))
    db.flush()
    assert watchdog.check_staleness(db, now=now) is False
    events = _events(db)
    assert events and "daily bar" in events[0].message


def test_intraday_bars_do_not_count(db, monkeypatch):
    # Leftover intraday bars from the retired rolling scan must not mask —
    # or trigger — staleness; only 1Day bars are maintained now.
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    db.add(_bar(now, timeframe="15Min"))
    db.flush()
    assert watchdog.check_staleness(db, now=now) is False


def test_alert_cooldown_suppresses_repeat_warnings(db, monkeypatch):
    monkeypatch.setattr(watchdog, "is_market_open", lambda now=None: True)
    now = datetime.now(UTC)
    assert watchdog.check_staleness(db, now=now) is False
    assert watchdog.check_staleness(db, now=now + timedelta(minutes=5)) is False
    assert len(_events(db)) == 1  # one episode, one event
    assert (
        watchdog.check_staleness(db, now=now + watchdog.ALERT_COOLDOWN + timedelta(minutes=1))
        is False
    )
    assert len(_events(db)) == 2  # re-warned after the cooldown
