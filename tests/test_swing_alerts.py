"""Swing alert channel: SWING-labeled format + its OWN daily cap, separate
from the core cap. Telegram is always mocked — no network calls."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.db.models import AlertRow
from sentinel.pipeline.state import Signal
from sentinel.risk.engine import RiskCheckResult
from sentinel.swing import alerts as swing_alerts
from sentinel.swing.alerts import format_swing_alert, route_swing_alerts

CREATED = datetime(2026, 7, 7, 14, 14, tzinfo=UTC)  # 10:14 AM EDT


def approved(symbol: str, action: str = "BUY") -> RiskCheckResult:
    return RiskCheckResult(
        approved=True, symbol=symbol, action=action, profile_version=1,
        checked_at=CREATED, rules=[],
    )


def make_signal(ticker="NVDA", action="BUY", confidence=0.93, ok=True, **over) -> Signal:
    check = approved(ticker, action)
    if not ok:
        check = check.model_copy(update={"approved": False})
    defaults = dict(
        created_at=CREATED,
        ticker=ticker,
        action=action,
        shares=18,
        max_entry_price=Decimal("875.20"),
        stop_loss=Decimal("842.10"),
        take_profit=Decimal("910.00"),
        confidence=confidence,
        expected_return_pct=4.8,
        risk_score=4,
        time_horizon="swing_days",
        strategy="swing-breakout",
        regime="bull-trend",
        book="swing",
        explanation="breakout above resistance on 2.1x volume",
        risk_check=check,
    )
    defaults.update(over)
    return Signal(**defaults)


@pytest.fixture()
def telegram_ok(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(swing_alerts, "telegram_configured", lambda db: True)
    monkeypatch.setattr(swing_alerts, "send_telegram", lambda db, text: (sent.append(text), (True, ""))[1])
    return sent


def test_swing_alert_is_labeled_and_disclaimed():
    text = format_swing_alert(make_signal())
    assert text.startswith("🟢 SWING BUY — NVDA")
    assert "Shares: 18" in text
    assert "Stop Loss: $842.10" in text
    assert "Target: $910.00" in text
    assert "Horizon: swing (3–10d)" in text
    assert text.endswith("Not financial advice. You place all trades.")


def test_router_filters_threshold_and_approval(db, telegram_ok):
    signals = [
        make_signal(ticker="AAA"),                   # eligible
        make_signal(ticker="BBB", confidence=0.70),  # below threshold
        make_signal(ticker="CCC", ok=False),         # vetoed
    ]
    assert route_swing_alerts(db, signals) == 1
    assert len(telegram_ok) == 1 and "AAA" in telegram_ok[0]
    rows = db.query(AlertRow).all()
    assert len(rows) == 1 and rows[0].kind == "swing_signal" and rows[0].ok


def test_router_has_its_own_cap(db, telegram_ok):
    signals = [make_signal(ticker=f"S{i:02d}") for i in range(6)]
    assert route_swing_alerts(db, signals) == 3  # default swing_max_alerts_per_day
    assert len(telegram_ok) == 3
    # a later run the same day sends nothing more
    assert route_swing_alerts(db, [make_signal(ticker="ZZZ")]) == 0


def test_router_skips_quietly_when_unconfigured(db, monkeypatch):
    monkeypatch.setattr(swing_alerts, "telegram_configured", lambda db: False)
    assert route_swing_alerts(db, [make_signal()]) == 0
    assert db.query(AlertRow).count() == 0
