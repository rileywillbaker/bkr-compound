"""News-triggered discovery: each deterministic trigger, candidate ranking/
capping, persistence, staleness, and scan-set assembly. No LLM anywhere."""

from datetime import UTC, date, datetime, timedelta

from sentinel.data.discovery import (
    DISCOVERY_KEY,
    DiscoveryParams,
    discover,
    get_candidates,
    get_scan_symbols,
    insider_net_shares,
)
from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FilingRow,
    InsiderTransactionRow,
    NewsItemRow,
    Position,
    SystemEvent,
)
from sentinel.db.settings_store import get_setting, set_setting, set_watchlist


def _seed_bars(db, symbol, last_volume=1_000_000, last_close_jump=0.0, days=30):
    """Flat daily series with an optional volume/price kick on the last bar."""
    base_ts = datetime.now(UTC).replace(hour=21, minute=0, second=0, microsecond=0)
    close = 100.0
    for i in range(days, 0, -1):
        is_last = i == 1
        if is_last:
            close *= 1 + last_close_jump
        db.add(
            BarRow(
                symbol=symbol,
                timeframe="1Day",
                ts=base_ts - timedelta(days=i),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=last_volume if is_last else 1_000_000,
            )
        )
        close *= 1.001
    db.flush()


def test_earnings_surprise_trigger(db):
    db.add(
        EarningsCalendarRow(
            symbol="AAPL", date=date.today(), eps_estimate=1.00, eps_actual=1.30
        )
    )
    db.flush()
    result = discover(db)
    assert "AAPL" in result.candidates
    [event] = [e for e in result.events if e.kind == "earnings_surprise"]
    assert event.symbol == "AAPL" and "+30.0%" in event.detail


def test_high_impact_news_keyword_trigger(db):
    db.add(
        NewsItemRow(
            provider_id="n1",
            symbol="NVDA",
            headline="NVDA announces acquisition of chip startup",
            summary="",
            source="wire",
            url="",
            published_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    db.flush()
    result = discover(db)
    assert "NVDA" in result.candidates
    [event] = [e for e in result.events if e.kind == "high_impact_news"]
    assert "acquisition" in event.detail.lower()


def test_news_volume_spike_trigger(db):
    for i in range(6):
        db.add(
            NewsItemRow(
                provider_id=f"m{i}",
                symbol="MSFT",
                headline=f"routine headline {i}",
                summary="",
                source="wire",
                url="",
                published_at=datetime.now(UTC) - timedelta(hours=1 + i),
            )
        )
    db.flush()
    result = discover(db)
    assert any(
        e.kind == "high_impact_news" and e.symbol == "MSFT" for e in result.events
    )


def test_insider_cluster_trigger(db):
    for i, name in enumerate(["CEO A", "CFO B", "DIR C"]):
        db.add(
            InsiderTransactionRow(
                symbol="AMD",
                name=name,
                share_change=10_000 + i,
                transaction_date=date.today() - timedelta(days=3),
            )
        )
    db.flush()
    result = discover(db)
    assert "AMD" in result.candidates
    [event] = [e for e in result.events if e.kind == "insider_cluster"]
    assert "3 distinct insiders" in event.detail


def test_insider_sellers_do_not_cluster(db):
    for i, name in enumerate(["CEO A", "CFO B", "DIR C"]):
        db.add(
            InsiderTransactionRow(
                symbol="AMD",
                name=name,
                share_change=-10_000 - i,
                transaction_date=date.today() - timedelta(days=3),
            )
        )
    db.flush()
    result = discover(db)
    assert not [e for e in result.events if e.kind == "insider_cluster"]


def test_unusual_volume_and_big_move_triggers(db):
    _seed_bars(db, "TSLA", last_volume=5_000_000, last_close_jump=0.08)
    result = discover(db)
    kinds = {e.kind for e in result.events if e.symbol == "TSLA"}
    assert "unusual_volume" in kinds
    assert "macro_move" in kinds
    assert "TSLA" in result.candidates


def test_quiet_symbol_is_not_a_candidate(db):
    _seed_bars(db, "KO")  # flat price, flat volume
    result = discover(db)
    assert "KO" not in result.candidates


def test_fresh_filing_trigger(db):
    db.add(
        FilingRow(
            accession_no="acc-1",
            symbol="META",
            cik="0001",
            form="8-K",
            filed_at=date.today(),
            description="material definitive agreement",
        )
    )
    db.flush()
    result = discover(db)
    assert any(e.kind == "fresh_filing" and e.symbol == "META" for e in result.events)


def test_candidates_capped_and_persisted_with_audit(db):
    for i in range(30):
        db.add(
            EarningsCalendarRow(
                symbol=f"A{i:03d}",  # not in universe -> ignored
                date=date.today(),
                eps_estimate=1.0,
                eps_actual=2.0,
            )
        )
    db.add(
        EarningsCalendarRow(
            symbol="AAPL", date=date.today(), eps_estimate=1.0, eps_actual=2.0
        )
    )
    db.flush()
    result = discover(db, DiscoveryParams(max_candidates=5))
    assert len(result.candidates) <= 5
    assert result.candidates == ["AAPL"]  # non-universe tickers never surface

    stored = get_setting(db, DISCOVERY_KEY)
    assert stored["candidates"] == ["AAPL"]
    assert get_candidates(db) == ["AAPL"]
    events = db.query(SystemEvent).filter(SystemEvent.kind == "discovery.run").all()
    assert len(events) == 1 and events[0].payload["candidates"] == ["AAPL"]


def test_stale_candidates_expire(db):
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    set_setting(db, DISCOVERY_KEY, {"as_of": old, "candidates": ["AAPL"]})
    assert get_candidates(db) == []


def test_scan_symbols_union(db):
    set_setting(
        db,
        DISCOVERY_KEY,
        {"as_of": datetime.now(UTC).isoformat(), "candidates": ["RGTI"]},
    )
    set_watchlist(db, ["NVDA"])
    db.add(Position(symbol="AAPL", shares=5, cost_basis=100))
    db.flush()
    # candidates + highlighted watchlist + held positions — never capped by
    # the watchlist
    assert get_scan_symbols(db) == ["AAPL", "NVDA", "RGTI"]


def test_insider_net_shares(db):
    assert insider_net_shares(db, "AMD") is None  # nothing ingested
    db.add(
        InsiderTransactionRow(
            symbol="AMD",
            name="CEO A",
            share_change=1000,
            transaction_date=date.today() - timedelta(days=10),
        )
    )
    db.add(
        InsiderTransactionRow(
            symbol="AMD",
            name="CFO B",
            share_change=-250,
            transaction_date=date.today() - timedelta(days=5),
        )
    )
    db.flush()
    assert insider_net_shares(db, "AMD") == 750
