"""End-to-end swing pipeline (golden-file style): synthetic bars seeded into
the DB, the full swing run, LLM mocked everywhere. Proves book="swing"
tagging, the shared risk gate (approve + veto), and feed separation from the
long-term ("core") book.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sentinel.agents import analysts as analysts_mod
from sentinel.agents import review as review_mod
from sentinel.agents.review import LLMReviewPayload
from sentinel.agents.verdicts import EvidenceItem, LLMVerdictPayload
from sentinel.api.main import create_app
from sentinel.db.base import get_db
from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FundamentalsRow,
    MacroSeriesRow,
    SignalRow,
    SystemEvent,
)
from sentinel.pipeline.runner import run_scan
from sentinel.strategies import selector as selector_mod
from sentinel.strategies.selector import _TieBreakVote
from sentinel.swing.pipeline import run_swing_scan
from sentinel.swing.screen import SwingScreenParams
from tests.synth import make_bars

# Movement floor bypassed: linear synthetic bars have a small ATR%; the ATR
# floor itself is covered in test_swing_screen. Everything else is real.
PARAMS = SwingScreenParams(min_atr_pct=0.0)


def seed_bars(db, symbol, **kwargs):
    bars = make_bars(symbol, 250, **kwargs)
    shift = datetime.now(UTC) - timedelta(days=1) - bars[-1].ts
    for b in bars:
        db.add(
            BarRow(
                symbol=symbol,
                timeframe="1Day",
                ts=b.ts + shift,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
        )
    db.flush()


@pytest.fixture()
def seeded(db):
    seed_bars(db, "SPY", start=400.0, drift=0.5)
    # Uptrend into a fresh high with a closing volume spike → breakout setup.
    seed_bars(db, "NVDA", start=100.0, drift=0.3, last_volume=2_000_000)
    db.add(FundamentalsRow(symbol="NVDA", sector="Technology", market_cap=90_000.0))
    for i in range(10):
        db.add(
            MacroSeriesRow(
                series_id="VIXCLS", date=date.today() - timedelta(days=10 - i), value=15.0
            )
        )
    db.flush()
    return db


@pytest.fixture()
def llm_ok(monkeypatch):
    calls: list[str] = []

    def fake(db, role, system, user, schema, endpoint=""):
        calls.append(endpoint or role)
        if schema is LLMReviewPayload:
            return LLMReviewPayload(
                stance="confirm",
                explanation="Mocked swing rationale citing the data.",
                key_risks=[],
            )
        if schema is LLMVerdictPayload:
            return LLMVerdictPayload(
                score=45,
                confidence=0.8,
                summary="mocked interpretation",
                evidence=[EvidenceItem(source="mock", datapoint="mocked datapoint")],
            )
        if schema is _TieBreakVote:
            return _TieBreakVote(strategy="cash", reason="mock vote")
        raise AssertionError(f"unexpected schema {schema}")

    for mod in (analysts_mod, selector_mod, review_mod):
        monkeypatch.setattr(mod, "complete_json", fake)
    return calls


def test_swing_scan_spends_at_most_one_call_per_finalist(seeded, llm_ok):
    """The swing book runs twice a day — a per-candidate fan-out here would be
    the most expensive thing in the system. It must not happen."""
    run_swing_scan(seeded, symbols=["NVDA"], params=PARAMS, enrich=False)
    assert llm_ok in ([], ["review.candidate"])
    assert all(endpoint == "review.candidate" for endpoint in llm_ok)


def test_swing_scan_tags_book_and_persists(seeded, llm_ok):
    result = run_swing_scan(seeded, symbols=["NVDA"], params=PARAMS, enrich=False)
    assert result.regime == "bull-trend"
    assert "NVDA" in result.screened
    assert result.signals

    sig = result.signals[0]
    assert sig.ticker == "NVDA"
    assert sig.book == "swing"
    assert sig.strategy.startswith("swing-")
    assert sig.time_horizon == "swing_days"

    row = seeded.get(SignalRow, str(sig.id))
    assert row is not None and row.book == "swing"

    # the swing run is recorded in the audit trail
    events = seeded.execute(
        select(SystemEvent).where(SystemEvent.kind == "swing.run")
    ).scalars().all()
    assert len(events) == 1 and events[0].payload["regime"] == "bull-trend"


def test_swing_breakout_is_actionable_buy(seeded, llm_ok):
    result = run_swing_scan(seeded, symbols=["NVDA"], params=PARAMS, enrich=False)
    buys = [s for s in result.signals if s.action == "BUY"]
    assert buys, "expected a swing BUY setup"
    buy = buys[0]
    assert buy.risk_check is not None and len(buy.risk_check.rules) == 11
    assert buy.risk_check.approved and buy.actionable
    # numeric levels come from deterministic sizing, never the LLM
    assert buy.shares and buy.shares > 0
    assert float(buy.stop_loss) < float(buy.take_profit)


def test_swing_earnings_blackout_vetoes(seeded, llm_ok):
    seeded.add(EarningsCalendarRow(symbol="NVDA", date=date.today() + timedelta(days=1)))
    seeded.flush()
    result = run_swing_scan(seeded, symbols=["NVDA"], params=PARAMS, enrich=False)
    buy = next(s for s in result.signals if s.action == "BUY")
    assert not buy.risk_check.approved
    assert "earnings_blackout_days" in buy.risk_check.failed_rules()
    assert not buy.actionable


def test_feeds_are_separate(seeded, llm_ok):
    run_scan(seeded, symbols=["NVDA"])  # core book
    run_swing_scan(seeded, symbols=["NVDA"], params=PARAMS, enrich=False)  # swing book
    seeded.commit()

    app = create_app()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    core = client.get("/api/signals").json()  # defaults to book=core
    assert core["signals"]
    assert all(s["book"] == "core" for s in core["signals"])

    swing = client.get("/api/trading").json()
    assert swing["signals"]
    assert all(s["book"] == "swing" for s in swing["signals"])
    assert swing["regime"] == "bull-trend"
