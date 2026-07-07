"""The five concrete strategies (spec §4.4).

Scores are additive rule points clamped to 0–100. Cash is always eligible and
carries a constant baseline — a candidate must beat "do nothing" on merit.
"""

from sentinel.agents.regime import RegimeName
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict
from sentinel.strategies.base import Strategy, StrategyFit, analyst_aggregate

CASH_BASELINE_SCORE = 30.0

_ALL_REGIMES: frozenset[RegimeName] = frozenset(
    {"bull-trend", "bear-trend", "range", "high-volatility"}
)


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, score))


class MomentumSwing(Strategy):
    name = "momentum-swing"
    eligible_regimes = frozenset({"bull-trend"})
    time_horizon = "swing_days"
    entry_logic = (
        "Buy strength in an uptrend: price above 20/50/200-day SMAs, MACD "
        "histogram positive, RSI 50–70, positive analyst aggregate."
    )
    exit_logic = "Exit at take-profit (≥2R), on close below the 20-day SMA, or at stop."
    stop_logic = "Initial stop 2×ATR14 below entry (risk profile governs the multiple)."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        if screen.trend_score > 0:
            score += screen.trend_score * 0.35
            reasons.append(f"trend score {screen.trend_score:+.0f}")
        if screen.momentum_score > 0:
            score += screen.momentum_score * 0.25
            reasons.append(f"momentum score {screen.momentum_score:+.0f}")
        if snap.rsi14 is not None and 50 <= snap.rsi14 <= 70:
            score += 15
            reasons.append(f"RSI in momentum band ({snap.rsi14:.0f})")
        if snap.macd_hist is not None and snap.macd_hist > 0:
            score += 10
            reasons.append("MACD histogram positive")
        agg = analyst_aggregate(verdicts)
        if agg > 0:
            score += min(20.0, agg * 0.4)
            reasons.append(f"analyst aggregate {agg:+.0f}")
        return StrategyFit(
            strategy=self.name,
            action="BUY",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class MeanReversion(Strategy):
    name = "mean-reversion"
    eligible_regimes = frozenset({"range", "bull-trend"})
    time_horizon = "swing_days"
    entry_logic = (
        "Buy an oversold dip toward support: RSI ≤ 35, price below the 20-day "
        "SMA but holding above the 200-day SMA, analysts not bearish."
    )
    exit_logic = "Exit at the 20-day SMA / prior range midpoint or at ≥2R take-profit."
    stop_logic = "Stop 2×ATR14 below entry, beneath the nearest swing low when close."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        if snap.rsi14 is not None and snap.rsi14 <= 35:
            score += 35
            reasons.append(f"RSI oversold ({snap.rsi14:.0f})")
        if snap.above_sma20 is False and snap.above_sma200 is True:
            score += 30
            reasons.append("dip below SMA20 within long-term uptrend (above SMA200)")
        if snap.support is not None and snap.close > 0:
            dist_pct = (snap.close - snap.support) / snap.close * 100
            if dist_pct <= 3:
                score += 15
                reasons.append(f"close within {dist_pct:.1f}% of swing support")
        agg = analyst_aggregate(verdicts)
        if agg >= 0:
            score += min(15.0, 5 + agg * 0.2)
            reasons.append(f"analysts not bearish ({agg:+.0f})")
        else:
            score -= 20
            reasons.append(f"analysts bearish ({agg:+.0f}) — reversion trade penalized")
        return StrategyFit(
            strategy=self.name,
            action="BUY",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class Breakout(Strategy):
    name = "breakout"
    eligible_regimes = frozenset({"bull-trend", "range"})
    time_horizon = "swing_days"
    entry_logic = (
        "Buy emerging strength through resistance: close within 3% of the "
        "52-week high or above nearest swing resistance, relative volume ≥ 1.5×."
    )
    exit_logic = "Exit at ≥2R take-profit or on a close back below the breakout level."
    stop_logic = "Stop 2×ATR14 below entry (just under the breakout level when close)."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        if snap.pct_from_52w_high is not None and snap.pct_from_52w_high >= -3:
            score += 35
            reasons.append(f"{snap.pct_from_52w_high:+.1f}% from 52-week high")
        if snap.resistance is None and snap.bars_used >= 60:
            score += 15
            reasons.append("no overhead swing resistance")
        if snap.relative_volume is not None and snap.relative_volume >= 1.5:
            score += 25
            reasons.append(f"relative volume {snap.relative_volume:.1f}×")
        if screen.volume_score > 0:
            score += screen.volume_score * 0.1
        agg = analyst_aggregate(verdicts)
        if agg > 0:
            score += min(15.0, agg * 0.3)
            reasons.append(f"analyst aggregate {agg:+.0f}")
        return StrategyFit(
            strategy=self.name,
            action="BUY",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class PositionHold(Strategy):
    name = "position-hold"
    eligible_regimes = frozenset({"bull-trend", "range"})
    time_horizon = "position_weeks"
    entry_logic = (
        "No new entry: constructive longer-term picture (above 200-day SMA, "
        "analysts positive) without a fresh swing setup — hold existing exposure."
    )
    exit_logic = "Re-evaluated daily; downgraded when the aggregate turns negative."
    stop_logic = "Existing position stops remain in force; no new risk is added."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons: list[str] = []
        score = 0.0
        agg = analyst_aggregate(verdicts)
        if snap.above_sma200 is True:
            score += 25
            reasons.append("above 200-day SMA")
        if agg > 10:
            score += 25
            reasons.append(f"analysts positive ({agg:+.0f})")
        # explicitly weaker than a real setup: caps below typical entry scores
        return StrategyFit(
            strategy=self.name,
            action="HOLD",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


class Cash(Strategy):
    name = "cash"
    eligible_regimes = _ALL_REGIMES  # always eligible (spec §4.4)
    time_horizon = "swing_days"
    entry_logic = "No trade. Default whenever no strategy setup clearly beats doing nothing."
    exit_logic = "Not applicable."
    stop_logic = "Not applicable."

    def evaluate(self, snap, screen, verdicts, regime):
        reasons = ["cash baseline — a setup must beat doing nothing"]
        score = CASH_BASELINE_SCORE
        if regime == "high-volatility":
            score += 30
            reasons.append("high-volatility regime favors standing aside")
        if not screen.eligible:
            score += 30
            reasons.append("candidate failed screener filters")
        return StrategyFit(
            strategy=self.name,
            action="NO_TRADE",
            score=_clamp(score),
            reasons=reasons,
            time_horizon=self.time_horizon,
        )


ALL_STRATEGIES: list[Strategy] = [
    MomentumSwing(),
    MeanReversion(),
    Breakout(),
    PositionHold(),
    Cash(),
]

_BY_NAME = {s.name: s for s in ALL_STRATEGIES}


def get_strategy(name: str) -> Strategy:
    return _BY_NAME[name]


def evaluate_all(
    snap: TechnicalSnapshot,
    screen: ScreenResult,
    verdicts: list[AnalystVerdict],
    regime: RegimeName,
) -> list[StrategyFit]:
    """Fits for every regime-eligible strategy (cash always included)."""
    return [
        s.evaluate(snap, screen, verdicts, regime) for s in ALL_STRATEGIES if s.eligible(regime)
    ]
