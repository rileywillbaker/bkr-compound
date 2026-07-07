"""End-to-end pipeline (golden-file style): synthetic fixture data seeded into
the DB, the full LangGraph run, mocked LLM everywhere (spec: mock LLM in CI).

Covers the happy path (approved BUY), the risk-veto path, LLM-outage
degradation, and the API router wiring.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sentinel.agents import analysts as analysts_mod
from sentinel.agents.verdicts import EvidenceItem, LLMVerdictPayload
from sentinel.api.main import create_app
from sentinel.db.base import get_db
from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FundamentalsRow,
    MacroSeriesRow,
    SystemEvent,
)
from sentinel.pipeline import runner
from sentinel.pipeline import synthesizer as synthesizer_mod
from sentinel.pipeline.runner import run_scan
from sentinel.pipeline.synthesizer import _Explanation
from sentinel.providers.llm.client import LLMError
from sentinel.strategies import selector as selector_mod
from sentinel.strategies.selector import _TieBreakVote
from tests.synth import make_bars

SYMBOLS = ["NVDA"]


def seed_bars(db, symbol, **kwargs):
    """Daily bars ending yesterday so build_market_context's lookback sees them."""
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
    seed_bars(db, "NVDA", start=100.0, drift=0.3)
    db.add(
        FundamentalsRow(
            symbol="NVDA", sector="Technology", market_cap=90_000.0, pe=30.0, ps=12.0
        )
    )
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
    """One fake LLM serving all three call sites, dispatching on schema."""

    def fake(db, role, system, user, schema, endpoint=""):
        if schema is LLMVerdictPayload:
            return LLMVerdictPayload(
                score=42,
                confidence=0.8,
                summary="mocked interpretation",
                evidence=[EvidenceItem(source="mock", datapoint="mocked datapoint")],
            )
        if schema is _Explanation:
            return _Explanation(text="Mocked plain-English rationale citing the data.")
        if schema is _TieBreakVote:
            return _TieBreakVote(strategy="cash", reason="mock vote")
        raise AssertionError(f"unexpected schema {schema}")

    for mod in (analysts_mod, selector_mod, synthesizer_mod):
        monkeypatch.setattr(mod, "complete_json", fake)


@pytest.fixture()
def llm_down(monkeypatch):
    def fake(*args, **kwargs):
        raise LLMError("outage")

    for mod in (analysts_mod, selector_mod, synthesizer_mod):
        monkeypatch.setattr(mod, "complete_json", fake)


def test_golden_run_produces_approved_buy(seeded, llm_ok):
    state = run_scan(seeded, symbols=SYMBOLS)

    assert state.regime is not None and state.regime.regime == "bull-trend"
    assert state.candidates["NVDA"].screen.eligible

    [signal] = state.signals
    assert signal.ticker == "NVDA"
    assert signal.action == "BUY"
    assert signal.strategy == "momentum-swing"
    assert signal.time_horizon == "swing_days"
    assert signal.explanation == "Mocked plain-English rationale citing the data."
    assert not signal.deterministic_only
    assert 0 < signal.confidence < 1
    assert 1 <= signal.risk_score <= 10
    assert signal.evidence

    # numeric fields are consistent with deterministic sizing, never the LLM:
    snap = state.candidates["NVDA"].snapshot
    sizing = state.candidates["NVDA"].sizing
    assert signal.shares == sizing.shares > 0
    assert float(signal.stop_loss) == sizing.stop_loss < snap.close
    assert float(signal.take_profit) == sizing.take_profit > snap.close
    # 2R: reward distance is twice the stop distance
    reward = sizing.take_profit - snap.close
    risk = snap.close - sizing.stop_loss
    assert abs(reward - 2 * risk) < 1e-6
    # position respects the 10% cap against default 10k starting equity
    assert sizing.shares * snap.close <= 1_000 + snap.close

    # risk gate ran every rule and approved
    assert signal.risk_check is not None
    assert signal.risk_check.approved
    assert len(signal.risk_check.rules) == 11
    assert signal.actionable

    # audit trail records the run
    run_events = seeded.execute(
        select(SystemEvent).where(SystemEvent.kind == "pipeline.run")
    ).scalars().all()
    assert len(run_events) == 1
    assert run_events[0].payload["regime"] == "bull-trend"

    # the signal + its full risk check are persisted (Phase 4)
    from sentinel.db.models import RiskCheckRow, SignalRow

    row = seeded.get(SignalRow, str(signal.id))
    assert row is not None and row.run_id == str(state.run_id)
    checks = seeded.execute(select(RiskCheckRow)).scalars().all()
    assert len(checks) == 1 and checks[0].approved and len(checks[0].rules) == 11


def test_earnings_blackout_vetoes_buy(seeded, llm_ok):
    seeded.add(
        EarningsCalendarRow(symbol="NVDA", date=date.today() + timedelta(days=1))
    )
    seeded.flush()
    state = run_scan(seeded, symbols=SYMBOLS)
    [signal] = state.signals
    assert signal.action == "BUY"
    assert signal.risk_check is not None
    assert not signal.risk_check.approved
    assert "earnings_blackout_days" in signal.risk_check.failed_rules()
    assert not signal.actionable

    rejected = seeded.execute(
        select(SystemEvent).where(SystemEvent.kind == "signal.rejected")
    ).scalars().all()
    assert len(rejected) == 1
    assert rejected[0].payload["signal_id"] == str(signal.id)


def test_llm_outage_degrades_to_deterministic(seeded, llm_down):
    state = run_scan(seeded, symbols=SYMBOLS)
    [signal] = state.signals
    assert signal.deterministic_only
    assert "LLM narrative unavailable" in signal.explanation
    # verdicts fell back but the pipeline still completed with a risk check
    assert all(v.deterministic_only for v in state.candidates["NVDA"].verdicts)
    assert signal.risk_check is not None


def test_use_llm_false_never_touches_llm(seeded, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM must not be called")

    for mod in (analysts_mod, selector_mod, synthesizer_mod):
        monkeypatch.setattr(mod, "complete_json", boom)
    state = run_scan(seeded, symbols=SYMBOLS, use_llm=False)
    [signal] = state.signals
    assert signal.deterministic_only


def test_pipeline_router(seeded, llm_ok):
    app = create_app()

    def override_db():
        yield seeded

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    response = client.post("/api/pipeline/run", json={"symbols": ["NVDA"]})
    assert response.status_code == 200
    body = response.json()
    assert body["regime"] == "bull-trend"
    assert "disclaimer" in body and body["disclaimer"]
    assert len(body["signals"]) == 1
    assert body["signals"][0]["ticker"] == "NVDA"

    last = client.get("/api/pipeline/last")
    assert last.status_code == 200
    assert last.json()["run_id"] == body["run_id"]
    assert runner.last_run() is not None
