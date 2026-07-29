"""Portfolio manager: continuous, deterministic review of open positions.

A stock picker only answers "what should I buy?". A portfolio manager also
answers the questions that actually decide returns: when to trim, when to let
a winner run, when a thesis has broken, and when to do nothing. All of that is
arithmetic over data already in the database — zero LLM cost, so it can run on
every scan in every operating mode, including Free.

Recommended actions
-------------------
    EXIT                  thesis broken or stop level breached
    REDUCE                position outgrew the risk profile's size limit
    TAKE_PARTIAL_PROFITS  target reached; bank half, let the rest run
    TIGHTEN_STOP          1R+ in profit; trail the stop, never below breakeven
    INCREASE              add to a working position — only if the risk engine approves
    HOLD                  trend intact, nothing to do
    NO_ACTION             insufficient data to judge (never a trade by default)

Every recommendation that ADDS exposure is evaluated by the pure-Python risk
engine first and is dropped if the engine says no; there is no override path.
Recommendations that REDUCE exposure are always allowed to surface — refusing
to let the user cut risk would be the wrong kind of safety.

Nothing here executes anything. These are recommendations for the user's own
decision, like every other output of this system.
"""

import math
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.db.models import Position
from sentinel.portfolio.sizing import size_position
from sentinel.portfolio.state import build_portfolio_state, compute_correlations
from sentinel.risk.engine import CandidateOrder, PortfolioState, RiskCheckResult
from sentinel.risk.engine import evaluate as risk_evaluate
from sentinel.risk.profile import RiskProfile
from sentinel.risk.store import get_active_profile

log = structlog.get_logger()

PositionAction = Literal[
    "EXIT",
    "REDUCE",
    "TAKE_PARTIAL_PROFITS",
    "TIGHTEN_STOP",
    "INCREASE",
    "HOLD",
    "NO_ACTION",
]

# Actions that reduce exposure and therefore become SELL signals downstream.
SELLING_ACTIONS: frozenset[str] = frozenset({"EXIT", "REDUCE", "TAKE_PARTIAL_PROFITS"})

# Urgency drives ordering in the UI and briefs (5 = act today).
_URGENCY: dict[str, int] = {
    "EXIT": 5,
    "REDUCE": 4,
    "TAKE_PARTIAL_PROFITS": 3,
    "TIGHTEN_STOP": 2,
    "INCREASE": 2,
    "HOLD": 1,
    "NO_ACTION": 1,
}

# Only add to a position that still has plenty of headroom; adding into a
# nearly-full position is how concentration limits get breached by accident.
ADD_HEADROOM_FRACTION = 0.6


class PositionReview(BaseModel):
    """One position's deterministic verdict."""

    symbol: str
    action: PositionAction
    shares: int
    shares_delta: int = 0  # negative = sell N, positive = add N; 0 = no size change
    mark: float
    cost_basis: float
    market_value: float
    weight_pct: float
    unrealized_pct: float
    r_multiple: float | None = None  # profit measured in initial-risk units
    suggested_stop: float | None = None
    sector: str = ""
    reasons: list[str] = Field(default_factory=list)
    urgency: int = 1
    risk_check: RiskCheckResult | None = None  # present for INCREASE

    @property
    def is_sell(self) -> bool:
        return self.action in SELLING_ACTIONS and self.shares_delta < 0


def _weight_pct(market_value: float, equity: float) -> float:
    return round(market_value / equity * 100, 2) if equity > 0 else 0.0


def _evaluate_add(
    db: Session,
    symbol: str,
    sector: str,
    shares: int,
    entry_price: float,
    snap: TechnicalSnapshot,
    portfolio: PortfolioState,
    profile: RiskProfile,
) -> RiskCheckResult:
    """Run the real risk engine against a proposed add."""
    held = [p.symbol for p in portfolio.positions if p.shares != 0 and p.symbol != symbol]
    return risk_evaluate(
        CandidateOrder(
            symbol=symbol,
            action="BUY",
            shares=shares,
            entry_price=entry_price,
            sector=sector,
            avg_dollar_volume=snap.avg_dollar_volume20,
            atr_pct=snap.atr_pct,
            trading_days_to_earnings=None,
            correlations=compute_correlations(db, symbol, held) if held else {},
        ),
        portfolio,
        profile,
    )


def review_position(
    db: Session,
    symbol: str,
    shares: int,
    cost_basis: float,
    mark: float,
    sector: str,
    snap: TechnicalSnapshot | None,
    portfolio: PortfolioState,
    profile: RiskProfile,
    regime: RegimeAssessment | None = None,
    trading_days_to_earnings: int | None = None,
) -> PositionReview:
    """Deterministic verdict for one open position. No LLM, no I/O beyond the
    correlation lookup an INCREASE check requires."""
    market_value = shares * mark
    weight = _weight_pct(market_value, portfolio.equity)
    unrealized_pct = round((mark - cost_basis) / cost_basis * 100, 2) if cost_basis > 0 else 0.0

    base = PositionReview(
        symbol=symbol,
        action="NO_ACTION",
        shares=shares,
        mark=round(mark, 4),
        cost_basis=round(cost_basis, 4),
        market_value=round(market_value, 2),
        weight_pct=weight,
        unrealized_pct=unrealized_pct,
        sector=sector,
        urgency=_URGENCY["NO_ACTION"],
    )

    if snap is None or snap.atr14 is None or snap.atr14 <= 0 or cost_basis <= 0:
        base.reasons = ["not enough price history to judge this position"]
        return base

    stop_distance = profile.atr_stop_multiple * snap.atr14
    r_multiple = round((mark - cost_basis) / stop_distance, 2) if stop_distance > 0 else None
    base.r_multiple = r_multiple
    # Trailing stop never sits below breakeven once the trade is 1R onside.
    trailing = mark - stop_distance
    base.suggested_stop = round(max(trailing, cost_basis) if (r_multiple or 0) >= 1 else trailing, 4)

    reasons: list[str] = []
    if trading_days_to_earnings is not None and trading_days_to_earnings <= profile.earnings_blackout_days:
        reasons.append(f"earnings in {trading_days_to_earnings} trading day(s)")
    if regime is not None and regime.regime == "high-volatility":
        reasons.append("high-volatility regime — size and stops matter more than usual")

    # --- EXIT: thesis broken or the stop level is already breached -----------
    if r_multiple is not None and r_multiple <= -1.0:
        reasons.insert(0, f"stop level breached ({r_multiple:.2f}R, {unrealized_pct:+.1f}%)")
        return base.model_copy(
            update={
                "action": "EXIT",
                "shares_delta": -shares,
                "reasons": reasons,
                "urgency": _URGENCY["EXIT"],
            }
        )
    if snap.above_sma50 is False and snap.above_sma200 is False:
        reasons.insert(0, "trend broken: closed below both the 50- and 200-day averages")
        return base.model_copy(
            update={
                "action": "EXIT",
                "shares_delta": -shares,
                "reasons": reasons,
                "urgency": _URGENCY["EXIT"],
            }
        )
    if (
        r_multiple is not None
        and r_multiple >= profile.min_reward_risk
        and snap.above_sma20 is False
    ):
        reasons.insert(
            0, f"target reached ({r_multiple:.2f}R) and momentum rolling over below the 20-day average"
        )
        return base.model_copy(
            update={
                "action": "EXIT",
                "shares_delta": -shares,
                "reasons": reasons,
                "urgency": _URGENCY["EXIT"],
            }
        )

    # --- REDUCE: position outgrew the hard size limit ------------------------
    if weight > profile.max_position_pct and mark > 0:
        allowed = math.floor(portfolio.equity * profile.max_position_pct / 100 / mark)
        trim = max(1, shares - max(0, allowed))
        reasons.insert(
            0,
            f"position is {weight:.1f}% of equity, above the {profile.max_position_pct:.0f}% limit",
        )
        return base.model_copy(
            update={
                "action": "REDUCE",
                "shares_delta": -min(trim, shares),
                "reasons": reasons,
                "urgency": _URGENCY["REDUCE"],
            }
        )

    # --- TAKE PARTIAL: target hit, trend still intact ------------------------
    if r_multiple is not None and r_multiple >= profile.min_reward_risk and shares >= 2:
        reasons.insert(
            0,
            f"up {r_multiple:.2f}R ({unrealized_pct:+.1f}%) — at or past the "
            f"{profile.min_reward_risk:.1f}R target",
        )
        return base.model_copy(
            update={
                "action": "TAKE_PARTIAL_PROFITS",
                "shares_delta": -(shares // 2),
                "reasons": reasons,
                "urgency": _URGENCY["TAKE_PARTIAL_PROFITS"],
            }
        )

    # --- TIGHTEN STOP: 1R onside, protect it --------------------------------
    if r_multiple is not None and r_multiple >= 1.0:
        reasons.insert(
            0,
            f"up {r_multiple:.2f}R — trail the stop to ${base.suggested_stop:,.2f} "
            "(at or above breakeven)",
        )
        return base.model_copy(
            update={"action": "TIGHTEN_STOP", "reasons": reasons, "urgency": _URGENCY["TIGHTEN_STOP"]}
        )

    # --- INCREASE: working position with real headroom, risk engine willing --
    trend_intact = (
        snap.above_sma20 is True and snap.above_sma50 is True and snap.above_sma200 is True
    )
    quiet_regime = regime is None or regime.regime != "high-volatility"
    has_headroom = weight < profile.max_position_pct * ADD_HEADROOM_FRACTION
    no_earnings_risk = (
        trading_days_to_earnings is None
        or trading_days_to_earnings > profile.earnings_blackout_days
    )
    if (
        trend_intact
        and quiet_regime
        and has_headroom
        and no_earnings_risk
        and r_multiple is not None
        and r_multiple > 0
    ):
        sizing = size_position(portfolio.equity, mark, snap.atr14, profile)
        room_shares = math.floor(
            max(0.0, portfolio.equity * profile.max_position_pct / 100 - market_value) / mark
        )
        add = min(sizing.shares if sizing else 0, room_shares)
        if add > 0:
            check = _evaluate_add(
                db, symbol, sector, add, mark, snap, portfolio, profile
            )
            if check.approved:
                reasons.insert(
                    0,
                    f"working position ({r_multiple:.2f}R) still in a clean uptrend with "
                    f"room to {profile.max_position_pct:.0f}% of equity",
                )
                return base.model_copy(
                    update={
                        "action": "INCREASE",
                        "shares_delta": add,
                        "reasons": reasons,
                        "urgency": _URGENCY["INCREASE"],
                        "risk_check": check,
                    }
                )
            reasons.append(
                "an add was considered and vetoed by the risk engine: "
                + ", ".join(check.failed_rules())
            )

    # --- HOLD ----------------------------------------------------------------
    if snap.above_sma200 is True:
        reasons.insert(0, "trend intact — above the 200-day average, nothing to act on")
    else:
        reasons.insert(0, "below the 200-day average but above the stop — watching")
    return base.model_copy(
        update={"action": "HOLD", "reasons": reasons, "urgency": _URGENCY["HOLD"]}
    )


def review_positions(
    db: Session,
    snapshots: dict[str, TechnicalSnapshot] | None = None,
    regime: RegimeAssessment | None = None,
    earnings_days: dict[str, int] | None = None,
) -> list[PositionReview]:
    """Review every open position, most urgent first.

    `snapshots` lets the pipeline reuse technicals it already computed; when
    omitted they are computed here from stored bars (still zero API cost).
    """
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    if not portfolio.positions:
        return []

    snaps = dict(snapshots or {})
    missing = [p.symbol for p in portfolio.positions if p.symbol not in snaps]
    if missing:
        from sentinel.agents.technicals import compute_technicals
        from sentinel.data.context import build_market_context

        context = build_market_context(db, missing)
        for symbol, sym_ctx in context.symbols.items():
            snaps[symbol] = compute_technicals(symbol, sym_ctx.daily_bars)

    reviews = []
    for pos in portfolio.positions:
        row = db.get(Position, pos.symbol)
        cost_basis = float(row.cost_basis) if row else pos.price
        reviews.append(
            review_position(
                db,
                symbol=pos.symbol,
                shares=pos.shares,
                cost_basis=cost_basis,
                mark=pos.price,
                sector=pos.sector,
                snap=snaps.get(pos.symbol),
                portfolio=portfolio,
                profile=profile,
                regime=regime,
                trading_days_to_earnings=(earnings_days or {}).get(pos.symbol),
            )
        )
    return sorted(reviews, key=lambda r: (-r.urgency, r.symbol))
