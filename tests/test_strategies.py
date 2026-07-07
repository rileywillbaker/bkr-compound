"""Strategies + selector. LLM is always mocked; the tie-break is the only
LLM touchpoint and it may only ever choose a name from the tied set."""

import pytest

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import compute_technicals
from sentinel.agents.verdicts import AnalystVerdict
from sentinel.providers.llm.client import LLMError
from sentinel.strategies import selector as selector_mod
from sentinel.strategies.base import StrategyFit, analyst_aggregate
from sentinel.strategies.catalog import CASH_BASELINE_SCORE, evaluate_all
from sentinel.strategies.selector import _TieBreakVote, select_strategy
from tests.synth import make_bars

UPTREND_SNAP = compute_technicals("UP", make_bars("UP", 250, drift=0.3))
GOOD_SCREEN = ScreenResult(
    symbol="UP", eligible=True, momentum_score=60, trend_score=100, volume_score=20
)


def verdicts(score: int, confidence: float = 0.8) -> list[AnalystVerdict]:
    return [
        AnalystVerdict(analyst="technicals", symbol="UP", score=score, confidence=confidence)
    ]


def fits_by_name(fits: list[StrategyFit]) -> dict[str, StrategyFit]:
    return {f.strategy: f for f in fits}


def test_analyst_aggregate_ignores_unavailable():
    vs = [
        AnalystVerdict(analyst="a", symbol="X", score=80, confidence=0.5),
        AnalystVerdict(analyst="b", symbol="X", score=-100, confidence=0.0, unavailable=True),
    ]
    assert analyst_aggregate(vs) == 80.0
    assert analyst_aggregate([]) == 0.0


def test_regime_gates_eligibility():
    bull = fits_by_name(evaluate_all(UPTREND_SNAP, GOOD_SCREEN, verdicts(50), "bull-trend"))
    assert set(bull) == {"momentum-swing", "mean-reversion", "breakout", "position-hold", "cash"}
    bear = fits_by_name(evaluate_all(UPTREND_SNAP, GOOD_SCREEN, verdicts(50), "bear-trend"))
    assert set(bear) == {"cash"}  # cash is always eligible
    high_vol = fits_by_name(
        evaluate_all(UPTREND_SNAP, GOOD_SCREEN, verdicts(50), "high-volatility")
    )
    assert set(high_vol) == {"cash"}


def test_momentum_swing_scores_uptrend():
    fits = fits_by_name(evaluate_all(UPTREND_SNAP, GOOD_SCREEN, verdicts(50), "bull-trend"))
    momentum = fits["momentum-swing"]
    assert momentum.action == "BUY"
    assert momentum.score > CASH_BASELINE_SCORE
    assert momentum.reasons


def test_cash_boosted_in_high_vol_and_on_screen_failure():
    bad_screen = ScreenResult(symbol="UP", eligible=False, reasons=["too small"])
    [cash] = evaluate_all(UPTREND_SNAP, bad_screen, verdicts(0), "high-volatility")
    assert cash.strategy == "cash"
    assert cash.action == "NO_TRADE"
    assert cash.score == CASH_BASELINE_SCORE + 60


def test_mean_reversion_fires_on_oversold_dip():
    # long uptrend, then a sharp month-long dip: below SMA20, above SMA200
    up = make_bars("MR", 220, start=100.0, drift=0.5)
    dip = make_bars("MR", 30, start=100.0 + 0.5 * 219, drift=-1.2, start_day=220)
    snap = compute_technicals("MR", up + dip)
    assert snap.rsi14 is not None and snap.rsi14 <= 35
    assert snap.above_sma20 is False and snap.above_sma200 is True
    fits = fits_by_name(evaluate_all(snap, GOOD_SCREEN, verdicts(10), "range"))
    reversion = fits["mean-reversion"]
    assert reversion.score >= 65
    assert reversion.score > fits["cash"].score


REGIME = RegimeAssessment(regime="bull-trend")


def crafted_fits(*pairs: tuple[str, float]) -> list[StrategyFit]:
    actions = {"cash": "NO_TRADE", "position-hold": "HOLD"}
    return [
        StrategyFit(strategy=name, action=actions.get(name, "BUY"), score=score)
        for name, score in pairs
    ]


@pytest.fixture()
def no_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(selector_mod, "complete_json", boom)


def test_clear_winner_skips_llm(db, monkeypatch, no_llm):
    monkeypatch.setattr(
        selector_mod,
        "evaluate_all",
        lambda *a: crafted_fits(("momentum-swing", 80), ("cash", 30)),
    )
    selected = select_strategy(db, UPTREND_SNAP, GOOD_SCREEN, verdicts(50), REGIME)
    assert selected.fit.strategy == "momentum-swing"
    assert not selected.tie_break_used
    assert [f.strategy for f in selected.considered] == ["momentum-swing", "cash"]


def test_tie_break_llm_vote_wins(db, monkeypatch):
    monkeypatch.setattr(
        selector_mod,
        "evaluate_all",
        lambda *a: crafted_fits(("momentum-swing", 62), ("breakout", 60), ("cash", 30)),
    )
    monkeypatch.setattr(
        selector_mod,
        "complete_json",
        lambda *a, **k: _TieBreakVote(strategy="breakout", reason="volume regime favors it"),
    )
    selected = select_strategy(db, UPTREND_SNAP, GOOD_SCREEN, verdicts(50), REGIME)
    assert selected.fit.strategy == "breakout"
    assert selected.tie_break_used
    assert selected.tie_break_reason == "volume regime favors it"


def test_tie_break_vote_outside_set_falls_back_to_priority(db, monkeypatch):
    monkeypatch.setattr(
        selector_mod,
        "evaluate_all",
        lambda *a: crafted_fits(("momentum-swing", 62), ("breakout", 60)),
    )
    monkeypatch.setattr(
        selector_mod,
        "complete_json",
        lambda *a, **k: _TieBreakVote(strategy="cash", reason="not in tied set"),
    )
    selected = select_strategy(db, UPTREND_SNAP, GOOD_SCREEN, verdicts(50), REGIME)
    assert selected.fit.strategy == "momentum-swing"  # priority order, not the invalid vote
    assert selected.tie_break_used


def test_tie_break_llm_down_uses_priority(db, monkeypatch):
    monkeypatch.setattr(
        selector_mod,
        "evaluate_all",
        lambda *a: crafted_fits(("breakout", 61), ("cash", 60)),
    )

    def boom(*args, **kwargs):
        raise LLMError("outage")

    monkeypatch.setattr(selector_mod, "complete_json", boom)
    selected = select_strategy(db, UPTREND_SNAP, GOOD_SCREEN, verdicts(50), REGIME)
    assert selected.fit.strategy == "cash"  # cash outranks breakout in priority
    assert selected.tie_break_used


def test_use_llm_false_never_calls(db, monkeypatch, no_llm):
    monkeypatch.setattr(
        selector_mod,
        "evaluate_all",
        lambda *a: crafted_fits(("breakout", 61), ("momentum-swing", 60)),
    )
    selected = select_strategy(
        db, UPTREND_SNAP, GOOD_SCREEN, verdicts(50), REGIME, use_llm=False
    )
    assert selected.fit.strategy == "momentum-swing"  # priority beats raw score on tie
