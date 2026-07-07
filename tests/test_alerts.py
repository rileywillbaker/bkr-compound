"""Alert formatting, routing thresholds, rate limit, ops alerts, briefs.
Telegram is always mocked — no test performs a network call."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.alerts import router as alert_router
from sentinel.alerts.briefs import compose_post_close, compose_pre_open, send_brief
from sentinel.alerts.format import format_signal_alert
from sentinel.alerts.router import route_signal_alerts, send_ops_alert
from sentinel.db.models import AlertRow, Position, SignalRow
from sentinel.pipeline.persist import save_signals
from sentinel.pipeline.state import PipelineState, Signal
from sentinel.risk.engine import RiskCheckResult

CREATED = datetime(2026, 7, 7, 14, 14, tzinfo=UTC)  # 10:14 AM EDT


def approved_check(symbol: str, action: str = "BUY") -> RiskCheckResult:
    return RiskCheckResult(
        approved=True, symbol=symbol, action=action, profile_version=1,
        checked_at=CREATED, rules=[],
    )


def make_signal(
    ticker="NVDA",
    action="BUY",
    confidence=0.93,
    approved=True,
    shares=18,
    **overrides,
) -> Signal:
    check = approved_check(ticker, action)
    if not approved:
        check = check.model_copy(update={"approved": False})
    defaults = dict(
        created_at=CREATED,
        ticker=ticker,
        action=action,
        shares=shares,
        max_entry_price=Decimal("875.20"),
        stop_loss=Decimal("842.10"),
        take_profit=Decimal("910.00"),
        confidence=confidence,
        expected_return_pct=4.8,
        risk_score=4,
        time_horizon="swing_days",
        strategy="breakout",
        regime="bull-trend",
        explanation=(
            "institutional accumulation, bullish news flow, positive "
            "earnings revisions, breakout above $868 on 2.1x volume"
        ),
        risk_check=check,
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_buy_alert_matches_spec_format():
    text = format_signal_alert(make_signal())
    assert text == (
        "🟢 BUY ALERT — NVDA\n"
        "Shares: 18\n"
        "Max Price: $875.20\n"
        "Stop Loss: $842.10\n"
        "Target: $910.00\n"
        "Confidence: 93% | Risk: 4/10 | Horizon: swing (3–10d)\n"
        "Expected: +4.8%\n"
        "Why: institutional accumulation, bullish news flow, positive "
        "earnings revisions, breakout above $868 on 2.1x volume\n"
        "10:14 AM ET — Not financial advice. You place all trades."
    )


def test_sell_alert_states_shares_and_pnl():
    signal = make_signal(action="SELL", expected_return_pct=None)
    text = format_signal_alert(signal, realized_pnl=596.7)
    assert "🔴 SELL ALERT — NVDA" in text
    assert "Shares: 18" in text
    assert "Est. P&L: +$596.70" in text
    assert "Max Price" not in text
    assert text.endswith("Not financial advice. You place all trades.")


@pytest.fixture()
def telegram_ok(monkeypatch):
    sent: list[str] = []

    def fake_send(db, text):
        sent.append(text)
        return True, ""

    monkeypatch.setattr(alert_router, "telegram_configured", lambda db: True)
    monkeypatch.setattr(alert_router, "send_telegram", fake_send)
    return sent


def test_router_filters_threshold_approval_and_action(db, telegram_ok):
    signals = [
        make_signal(ticker="AAA"),                      # eligible
        make_signal(ticker="BBB", confidence=0.79),     # below threshold
        make_signal(ticker="CCC", approved=False),      # vetoed
        make_signal(ticker="DDD", action="HOLD", shares=None,
                    max_entry_price=None, stop_loss=None, take_profit=None),
    ]
    sent = route_signal_alerts(db, signals)
    assert sent == 1
    assert len(telegram_ok) == 1 and "AAA" in telegram_ok[0]
    assert signals[0].alert_sent
    assert not signals[1].alert_sent
    rows = db.query(AlertRow).all()
    assert len(rows) == 1 and rows[0].kind == "signal" and rows[0].ok


def test_router_rate_limit(db, telegram_ok):
    signals = [make_signal(ticker=f"S{i:02d}") for i in range(7)]
    sent = route_signal_alerts(db, signals)
    assert sent == 5  # default max_alerts_per_day
    assert len(telegram_ok) == 5
    # a later run the same day sends nothing more
    assert route_signal_alerts(db, [make_signal(ticker="ZZZ")]) == 0


def test_router_skips_quietly_when_unconfigured(db, monkeypatch):
    monkeypatch.setattr(alert_router, "telegram_configured", lambda db: False)
    assert route_signal_alerts(db, [make_signal()]) == 0
    assert db.query(AlertRow).count() == 0


def test_router_prefers_highest_confidence(db, telegram_ok, monkeypatch):
    monkeypatch.setattr(
        alert_router, "get_settings",
        lambda: type("S", (), {"alert_confidence_threshold": 0.8, "max_alerts_per_day": 1})(),
    )
    route_signal_alerts(db, [make_signal(ticker="LOW", confidence=0.85),
                             make_signal(ticker="TOP", confidence=0.99)])
    assert len(telegram_ok) == 1 and "TOP" in telegram_ok[0]


def test_sell_pnl_uses_cost_basis(db, telegram_ok):
    db.add(Position(symbol="NVDA", shares=18, cost_basis=Decimal("842.05")))
    db.flush()
    route_signal_alerts(db, [make_signal(action="SELL")])
    [text] = telegram_ok
    assert "Est. P&L: +$596.70" in text  # (875.20 - 842.05) * 18


def test_alert_sent_persisted_on_signal_row(db, telegram_ok):
    signal = make_signal()
    state = PipelineState(symbols=["NVDA"], signals=[signal])
    save_signals(db, state)
    route_signal_alerts(db, [signal])
    assert db.get(SignalRow, str(signal.id)).alert_sent


def test_ops_alert_recorded(db, telegram_ok):
    assert send_ops_alert(db, "⚠️ data stale")
    [row] = db.query(AlertRow).all()
    assert row.kind == "ops" and row.ok and "stale" in row.text


def test_briefs(db, monkeypatch):
    from tests.test_pipeline import seed_bars

    seed_bars(db, "SPY", start=400.0, drift=0.5)
    monkeypatch.setattr(
        "sentinel.alerts.briefs.get_settings",
        lambda: type("S", (), {"watchlist_symbols": ["SPY"], "starting_equity": 10_000.0})(),
    )
    monkeypatch.setattr("sentinel.alerts.briefs.telegram_configured", lambda db: True)
    monkeypatch.setattr("sentinel.alerts.briefs.send_telegram", lambda db, text: (True, ""))
    pre = compose_pre_open(db)
    assert "Regime:" in pre and "Equity:" in pre
    assert pre.endswith("Not financial advice. You place all trades.")

    signal = make_signal()
    save_signals(db, PipelineState(symbols=["NVDA"], signals=[signal]))
    post = compose_post_close(db)
    assert "Signals today: 1 (1 BUY/SELL)" in post
    assert "BUY NVDA conf 93% — pending" in post

    text = send_brief(db, "post_close")
    assert text is not None
    rows = db.query(AlertRow).filter(AlertRow.kind == "brief_post_close").all()
    assert len(rows) == 1 and rows[0].ok


def test_brief_unknown_kind_raises(db):
    with pytest.raises(ValueError):
        send_brief(db, "midday")
