"""Swing strategies + deterministic selector. No LLM anywhere in swing
selection — these are pure functions of the computed snapshot."""

from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict
from sentinel.strategies.base import StrategyFit
from sentinel.swing import strategies as swing_mod
from sentinel.swing.strategies import (
    CASH_BASELINE_SCORE,
    evaluate_all_swing,
    select_swing_strategy,
)

GOOD_SCREEN = ScreenResult(
    symbol="X", eligible=True, momentum_score=40, trend_score=80, volume_score=30
)
BAD_SCREEN = ScreenResult(symbol="X", eligible=False, reasons=["too quiet for a swing"])


def snap(**kw) -> TechnicalSnapshot:
    base = dict(symbol="X", close=100.0, bars_used=250)
    base.update(kw)
    return TechnicalSnapshot(**base)


def verdicts(score: int = 20, confidence: float = 0.8) -> list[AnalystVerdict]:
    return [AnalystVerdict(analyst="technicals", symbol="X", score=score, confidence=confidence)]


def by_name(fits: list[StrategyFit]) -> dict[str, StrategyFit]:
    return {f.strategy: f for f in fits}


PULLBACK = snap(
    above_sma20=False, above_sma50=True, above_sma200=True, rsi14=48.0, support=97.0
)
BREAKOUT = snap(
    pct_from_52w_high=-1.0, relative_volume=2.0, above_sma50=True, resistance=None
)
OVERSOLD = snap(rsi14=28.0, above_sma200=True, support=98.0, above_sma50=False)
FLAT = snap(above_sma20=True, above_sma50=True, above_sma200=True, rsi14=62.0)


def test_regime_gates_eligibility():
    bull = by_name(evaluate_all_swing(PULLBACK, GOOD_SCREEN, verdicts(), "bull-trend"))
    assert set(bull) == {"swing-pullback", "swing-breakout", "swing-oversold", "swing-cash"}
    for closed in ("bear-trend", "high-volatility"):
        only = by_name(evaluate_all_swing(PULLBACK, GOOD_SCREEN, verdicts(), closed))
        assert set(only) == {"swing-cash"}  # cash is always eligible


def test_pullback_fires_on_dip_in_uptrend():
    fits = by_name(evaluate_all_swing(PULLBACK, GOOD_SCREEN, verdicts(), "bull-trend"))
    pull = fits["swing-pullback"]
    assert pull.action == "BUY"
    assert pull.score > 60
    assert pull.score > fits["swing-cash"].score
    assert pull.reasons


def test_breakout_fires_near_high_on_volume():
    fits = by_name(evaluate_all_swing(BREAKOUT, GOOD_SCREEN, verdicts(), "bull-trend"))
    brk = fits["swing-breakout"]
    assert brk.action == "BUY"
    assert brk.score > 60
    assert brk.score > fits["swing-cash"].score


def test_oversold_fires_on_washed_out_uptrend():
    fits = by_name(evaluate_all_swing(OVERSOLD, GOOD_SCREEN, verdicts(), "range"))
    osc = fits["swing-oversold"]
    assert osc.action == "BUY"
    assert osc.score > 60


def test_setups_capped_low_without_their_signature():
    # A calm, extended name: no pullback, no breakout, no washout → all setups
    # capped below cash, so cash wins.
    fits = by_name(evaluate_all_swing(FLAT, GOOD_SCREEN, verdicts(), "bull-trend"))
    assert fits["swing-pullback"].score <= 25
    assert fits["swing-breakout"].score <= 25
    assert fits["swing-oversold"].score <= 25


def test_cash_boosted_off_regime_and_on_screen_failure():
    [cash] = evaluate_all_swing(FLAT, BAD_SCREEN, verdicts(0), "high-volatility")
    assert cash.strategy == "swing-cash"
    assert cash.action == "NO_TRADE"
    assert cash.score == CASH_BASELINE_SCORE + 60  # +30 regime, +30 screen fail


def test_selector_picks_clear_winner():
    selected = select_swing_strategy(BREAKOUT, GOOD_SCREEN, verdicts(), "bull-trend")
    assert selected.fit.strategy == "swing-breakout"
    assert not selected.tie_break_used
    # ranked highest-first
    assert selected.considered[0].score >= selected.considered[-1].score


def test_selector_stands_aside_when_no_setup():
    selected = select_swing_strategy(FLAT, GOOD_SCREEN, verdicts(), "bull-trend")
    assert selected.fit.strategy == "swing-cash"
    assert selected.fit.action == "NO_TRADE"


def test_selector_deterministic_tie_break(monkeypatch):
    def crafted(*_a):
        return [
            StrategyFit(strategy="swing-breakout", action="BUY", score=62),
            StrategyFit(strategy="swing-pullback", action="BUY", score=60),
            StrategyFit(strategy="swing-cash", action="NO_TRADE", score=30),
        ]

    monkeypatch.setattr(swing_mod, "evaluate_all_swing", crafted)
    selected = select_swing_strategy(BREAKOUT, GOOD_SCREEN, verdicts(), "bull-trend")
    # within TIE_MARGIN (62 vs 60) → priority order prefers pullback over breakout
    assert selected.fit.strategy == "swing-pullback"
    assert selected.tie_break_used
