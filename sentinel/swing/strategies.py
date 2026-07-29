"""Swing strategies (2–10 day holds) + a deterministic selector.

Three long-only setups tuned for swing horizons plus an always-eligible cash
baseline, all built on the same `Strategy`/`StrategyFit` contract the core
catalog uses. `evaluate` is pure: it maps the candidate's computed facts to a
0–100 fit score with cited reasons. No LLM here — swing strategy selection is
fully deterministic (the LLM's only jobs downstream are the analyst
interpretations and the plain-English explanation, never a number and never
the choice of setup).

These deliberately do NOT touch sentinel/strategies/catalog.py: the long-term
book keeps its own five strategies untouched.
"""

from sentinel.agents.regime import RegimeName
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict
from sentinel.strategies.base import Strategy, StrategyFit, analyst_aggregate
from sentinel.strategies.selector import SelectedStrategy

CASH_BASELINE_SCORE = 30.0

# Swing setups only make sense in constructive tape; a downtrend or a
# high-volatility shock means stand aside (cash is always eligible).
_SWING_REGIMES: frozenset[RegimeName] = frozenset({"bull-trend", "range"})
_ALL_REGIMES: frozenset[RegimeName] = frozenset(
    {"bull-trend", "bear-trend", "range", "high-volatility"}
)

# Deterministic tie order when fits are within TIE_MARGIN: prefer standing
# aside, then the least aggressive setups.
TIE_MARGIN = 5.0
_TIE_PRIORITY = ["swing-cash", "swing-oversold", "swing-pullback", "swing-breakout"]


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, score))


class SwingPullback(Strategy):
    """Buy a pullback within an established uptrend."""

    name = "swing-pullback"
    eligible_regimes = _SWING_REGIMES
    time_horizon = "swing_days"
    entry_logic = (
        "Uptrend intact (price above the 50- and 200-day SMAs) but pulled back "
        "toward the rising 20-day SMA / prior support with RSI cooling to "
        "40–55; analysts not bearish."
    )
    exit_logic = "Take-profit at ≥2R, or exit on a close back below the 20-day SMA."
    stop_logic = "Stop 2×ATR14 below entry (risk profile governs the multiple)."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        uptrend = snap.above_sma50 is True and snap.above_sma200 is True
        if uptrend:
            score += 30
            reasons.append("uptrend intact (above 50 & 200-day SMA)")
        pulled_back = False
        if snap.rsi14 is not None and 40 <= snap.rsi14 <= 55:
            score += 20
            reasons.append(f"RSI cooled into pullback band ({snap.rsi14:.0f})")
            pulled_back = True
        if snap.above_sma20 is False and snap.above_sma50 is True:
            score += 15
            reasons.append("dip below the 20-day SMA while holding the 50-day")
            pulled_back = True
        if snap.support is not None and snap.close > 0:
            dist = (snap.close - snap.support) / snap.close * 100
            if dist <= 4:
                score += 15
                reasons.append(f"within {dist:.1f}% of swing support")
        agg = analyst_aggregate(verdicts)
        if agg >= 0:
            score += min(20.0, 5 + agg * 0.15)
            reasons.append(f"analysts constructive ({agg:+.0f})")
        else:
            score -= 15
            reasons.append(f"analysts bearish ({agg:+.0f}) — pullback penalized")
        # No genuine pullback (or a broken uptrend) is not this setup.
        if not (uptrend and pulled_back):
            score = min(score, 25.0)
        return StrategyFit(
            strategy=self.name,
            action="BUY",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class SwingBreakout(Strategy):
    """Buy a breakout/continuation through resistance on expanding volume."""

    name = "swing-breakout"
    eligible_regimes = _SWING_REGIMES
    time_horizon = "swing_days"
    entry_logic = (
        "Price within 3% of the 52-week high or clearing swing resistance, on "
        "relative volume ≥ 1.5×, with the 50-day SMA reclaimed."
    )
    exit_logic = "Take-profit at ≥2R, or exit on a close back below the breakout level."
    stop_logic = "Stop 2×ATR14 below entry (just under the breakout level when close)."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        near_high = snap.pct_from_52w_high is not None and snap.pct_from_52w_high >= -3
        if near_high:
            score += 35
            reasons.append(f"{snap.pct_from_52w_high:+.1f}% from 52-week high")
        if snap.resistance is None and snap.bars_used >= 60:
            score += 10
            reasons.append("no overhead swing resistance")
        strong_volume = snap.relative_volume is not None and snap.relative_volume >= 1.5
        if strong_volume:
            score += 25
            reasons.append(f"relative volume {snap.relative_volume:.1f}×")
        if snap.above_sma50 is True:
            score += 10
            reasons.append("above the 50-day SMA")
        agg = analyst_aggregate(verdicts)
        if agg > 0:
            score += min(15.0, agg * 0.25)
            reasons.append(f"analyst aggregate {agg:+.0f}")
        # A breakout needs the breakout: proximity to highs AND volume expansion.
        if not (near_high and strong_volume):
            score = min(score, 25.0)
        return StrategyFit(
            strategy=self.name,
            action="BUY",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class SwingOversold(Strategy):
    """Buy a deeply oversold bounce inside an intact longer-term uptrend."""

    name = "swing-oversold"
    eligible_regimes = _SWING_REGIMES
    time_horizon = "swing_days"
    entry_logic = (
        "RSI ≤ 30 into support while price holds above the 200-day SMA "
        "(long-term uptrend intact); analysts not bearish."
    )
    exit_logic = "Exit at the 20-day SMA / prior range midpoint or at ≥2R take-profit."
    stop_logic = "Stop 2×ATR14 below entry, beneath the nearest swing low when close."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        deep = snap.rsi14 is not None and snap.rsi14 <= 30
        mild = snap.rsi14 is not None and snap.rsi14 <= 35
        if deep:
            score += 40
            reasons.append(f"deeply oversold (RSI {snap.rsi14:.0f})")
        elif mild:
            score += 25
            reasons.append(f"oversold (RSI {snap.rsi14:.0f})")
        uptrend = snap.above_sma200 is True
        if uptrend:
            score += 25
            reasons.append("long-term uptrend intact (above 200-day SMA)")
        if snap.support is not None and snap.close > 0:
            dist = (snap.close - snap.support) / snap.close * 100
            if dist <= 3:
                score += 15
                reasons.append(f"within {dist:.1f}% of swing support")
        agg = analyst_aggregate(verdicts)
        if agg >= 0:
            score += min(10.0, 5 + agg * 0.1)
            reasons.append(f"analysts not bearish ({agg:+.0f})")
        else:
            score -= 20
            reasons.append(f"analysts bearish ({agg:+.0f}) — no falling-knife buys")
        if not (mild and uptrend):
            score = min(score, 25.0)
        return StrategyFit(
            strategy=self.name,
            action="BUY",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class SwingCash(Strategy):
    """No trade — the default whenever no setup clearly beats standing aside."""

    name = "swing-cash"
    eligible_regimes = _ALL_REGIMES
    time_horizon = "swing_days"
    entry_logic = "No trade. A swing setup must beat doing nothing on merit."
    exit_logic = "Not applicable."
    stop_logic = "Not applicable."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons = ["cash baseline — a setup must beat doing nothing"]
        score = CASH_BASELINE_SCORE
        if regime not in _SWING_REGIMES:
            score += 30
            reasons.append(f"{regime} regime favors standing aside for swings")
        if not screen.eligible:
            score += 30
            reasons.append("candidate failed the swing screen")
        return StrategyFit(
            strategy=self.name,
            action="NO_TRADE",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


ALL_SWING_STRATEGIES: list[Strategy] = [
    SwingPullback(),
    SwingBreakout(),
    SwingOversold(),
    SwingCash(),
]


def evaluate_all_swing(
    snap: TechnicalSnapshot,
    screen: ScreenResult,
    verdicts: list[AnalystVerdict],
    regime: RegimeName,
) -> list[StrategyFit]:
    """Fits for every regime-eligible swing strategy (cash always included)."""
    return [
        s.evaluate(snap, screen, verdicts, regime)
        for s in ALL_SWING_STRATEGIES
        if s.eligible(regime)
    ]


def _priority_pick(fits: list[StrategyFit]) -> StrategyFit:
    return min(fits, key=lambda f: _TIE_PRIORITY.index(f.strategy))


def select_swing_strategy(
    snap: TechnicalSnapshot,
    screen: ScreenResult,
    verdicts: list[AnalystVerdict],
    regime: RegimeName,
) -> SelectedStrategy:
    """Highest deterministic fit wins; ties (within TIE_MARGIN) break by the
    fixed priority order — no LLM. Returns the core SelectedStrategy shape so
    the shared synthesizer consumes it unchanged."""
    fits = evaluate_all_swing(snap, screen, verdicts, regime)
    ranked = sorted(fits, key=lambda f: f.score, reverse=True)
    top = ranked[0]
    tied = [f for f in ranked if top.score - f.score < TIE_MARGIN]
    if len(tied) == 1:
        return SelectedStrategy(fit=top, considered=ranked)
    return SelectedStrategy(
        fit=_priority_pick(tied),
        considered=ranked,
        tie_break_used=True,
        tie_break_reason="deterministic swing priority order",
    )
