"""Turn a ranked idea into a dollar amount — through the risk engine, never around it.

The report speaks in dollars because that is what the user asked for and what
is actually actionable ("$35 of UEC", not "1.4% of the book"). But a dollar
amount is a trade parameter, so it obeys the project's hardest rule: it comes
only from deterministic code — position sizing, the pure-Python risk engine,
and the portfolio state — and never from a language model.

The chain for every recommendation:

    1. size_position()          fixed-fractional sizing from ATR and the
                                active risk profile (portfolio/sizing.py)
    2. cash cap                 never propose more than the account holds
    3. risk engine evaluate()   the real engine, all rules, absolute veto
    4. portfolio manager check  existing exposure and correlation context

Step 2 deserves a note. The risk engine deliberately knows nothing about cash
— it governs risk, not settlement — so an uncapped suggestion could exceed the
balance while passing every rule. Rather than add a rule to a pure, heavily
tested safety module, the cap is applied *here*, before the engine sees the
order. That only ever makes the proposal smaller, so the engine's veto remains
the final and strictest authority.

Fractional shares are handled honestly. On a small account the sizer may
return zero whole shares of an expensive stock; the allocation then reports
the risk-approved *notional* and states plainly that fractional shares are
required. It never rounds up to one share to make a recommendation possible.
"""

from __future__ import annotations

import math

import pandas as pd
import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.data.market_hours import trading_days_until
from sentinel.portfolio.sizing import SizingResult, size_position
from sentinel.portfolio.state import build_portfolio_state, cash_balance, compute_correlations
from sentinel.risk.engine import CandidateOrder, PortfolioState, RiskCheckResult
from sentinel.risk.engine import evaluate as risk_evaluate
from sentinel.risk.profile import RiskProfile
from sentinel.risk.store import get_active_profile

log = structlog.get_logger()

# Below this the recommendation is not worth acting on after commissions and
# spread, even when every rule passes.
MIN_ACTIONABLE_DOLLARS = 10.0


class Allocation(BaseModel):
    """A risk-approved dollar recommendation, or a documented refusal."""

    symbol: str
    approved: bool = False
    dollars: float = 0.0
    shares: int = 0
    fractional_required: bool = False
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    max_entry_price: float | None = None
    risk_amount: float | None = None
    reasons: list[str] = Field(default_factory=list)  # why it was rejected/trimmed
    failed_rules: list[str] = Field(default_factory=list)
    risk_check: RiskCheckResult | None = None
    existing_position_dollars: float = 0.0
    sector_exposure_pct: float | None = None
    max_correlation: float | None = None
    correlated_with: str = ""
    cash_available: float = 0.0

    @property
    def summary(self) -> str:
        if self.approved:
            note = " (fractional shares required)" if self.fractional_required else ""
            return f"${self.dollars:,.0f}{note}"
        return "no position — " + ("; ".join(self.reasons) or "risk engine declined")


def _dollar_cap_from_cash(cash: float) -> float:
    """Never recommend spending money that is not there.

    A small negative cash balance (possible if positions were entered manually)
    yields zero rather than a negative cap.
    """
    return max(0.0, cash)


def allocate(
    db: Session,
    symbol: str,
    price: float,
    snap: TechnicalSnapshot | None,
    sector: str,
    portfolio: PortfolioState | None = None,
    profile: RiskProfile | None = None,
    next_earnings: pd.Timestamp | None = None,
    cash: float | None = None,
) -> Allocation:
    """Deterministic dollar allocation for one BUY idea.

    Returns an Allocation whose `approved` is False — with reasons — far more
    often than True. That is the intended behaviour: NO TRADE is a first-class
    outcome everywhere in this system.
    """
    profile = profile or get_active_profile(db)
    portfolio = portfolio or build_portfolio_state(db)
    available_cash = _dollar_cap_from_cash(
        cash if cash is not None else cash_balance(db)
    )

    existing = portfolio.position_for(symbol)
    result = Allocation(
        symbol=symbol,
        price=round(price, 4),
        cash_available=round(available_cash, 2),
        existing_position_dollars=round(existing.market_value, 2) if existing else 0.0,
    )

    if price <= 0:
        result.reasons.append("no usable price")
        return result
    if snap is None or snap.atr14 is None or snap.atr14 <= 0:
        result.reasons.append("not enough price history to set a stop")
        return result
    if portfolio.equity <= 0:
        result.reasons.append("portfolio equity is not positive")
        return result

    sizing: SizingResult | None = size_position(portfolio.equity, price, snap.atr14, profile)
    if sizing is None:
        result.reasons.append(
            "position sizing returns zero shares at this price and volatility — "
            "the stop would be wider than the account can risk"
        )
        return result

    result.stop_loss = sizing.stop_loss
    result.take_profit = sizing.take_profit
    result.max_entry_price = sizing.max_entry_price
    result.risk_amount = sizing.risk_amount

    shares = sizing.shares
    notional = shares * price

    # --- cash cap (see module docstring) -----------------------------------
    if notional > available_cash:
        affordable = math.floor(available_cash / price)
        if affordable < shares:
            result.reasons.append(
                f"trimmed to available cash (${available_cash:,.0f})"
            )
        shares = max(0, affordable)
        notional = shares * price

    # --- sector and correlation context, for the report --------------------
    held = [p.symbol for p in portfolio.positions if p.shares != 0 and p.symbol != symbol]
    correlations = compute_correlations(db, symbol, held) if held else {}
    if correlations:
        worst = max(correlations.items(), key=lambda kv: kv[1])
        result.max_correlation = round(worst[1], 3)
        result.correlated_with = worst[0]
    if sector and portfolio.equity > 0:
        sector_value = sum(
            p.market_value for p in portfolio.positions if p.sector == sector
        )
        result.sector_exposure_pct = round(sector_value / portfolio.equity * 100, 2)

    # --- the real risk engine ---------------------------------------------
    # With zero whole shares affordable we still evaluate a one-share order:
    # that answers "would this trade be allowed at all?" (sector limits,
    # correlation, liquidity, earnings blackout) independently of size, and
    # a fractional recommendation is only made when the answer is yes.
    evaluated_shares = shares if shares > 0 else 1
    check = risk_evaluate(
        CandidateOrder(
            symbol=symbol,
            action="BUY",
            shares=evaluated_shares,
            entry_price=price,
            sector=sector,
            avg_dollar_volume=snap.avg_dollar_volume20,
            atr_pct=snap.atr_pct,
            trading_days_to_earnings=(
                trading_days_until(next_earnings) if next_earnings is not None else None
            ),
            correlations=correlations,
        ),
        portfolio,
        profile,
    )
    result.risk_check = check
    if not check.approved:
        result.failed_rules = check.failed_rules()
        result.reasons.append(
            "risk engine declined: " + ", ".join(check.failed_rules())
        )
        return result

    if shares > 0:
        result.shares = shares
        result.dollars = round(notional, 2)
    else:
        # Whole-share sizing is zero but the trade itself is permitted, so
        # express it as notional. The dollar figure is still bounded by every
        # deterministic cap above — sizing, cash, and max_position_pct.
        position_cap = portfolio.equity * profile.max_position_pct / 100
        existing_value = existing.market_value if existing else 0.0
        headroom = max(0.0, position_cap - existing_value)
        notional = min(available_cash, headroom, sizing.risk_amount / sizing.stop_distance * price)
        result.shares = 0
        result.fractional_required = True
        result.dollars = round(max(0.0, notional), 2)
        result.reasons.append(
            f"one whole share costs ${price:,.2f}; this size needs fractional shares"
        )

    if result.dollars < MIN_ACTIONABLE_DOLLARS:
        result.approved = False
        result.dollars = 0.0
        result.reasons.append(
            f"the approved size works out under ${MIN_ACTIONABLE_DOLLARS:,.0f} — "
            "too small to be worth the spread"
        )
        return result

    result.approved = True
    return result


def portfolio_context(db: Session) -> dict:
    """What the Portfolio Manager agent contributes to the report.

    Holdings, cash, exposure by sector and the count of open positions against
    the profile's limit — the facts the user needs to judge whether another
    position is sensible at all, independent of how good the idea is.
    """
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    cash = cash_balance(db)

    by_sector: dict[str, float] = {}
    for position in portfolio.positions:
        key = position.sector or "unclassified"
        by_sector[key] = by_sector.get(key, 0.0) + position.market_value

    return {
        "equity": round(portfolio.equity, 2),
        "cash": round(cash, 2),
        "positions": [
            {
                "symbol": p.symbol,
                "shares": p.shares,
                "value": round(p.market_value, 2),
                "sector": p.sector,
                "weight_pct": (
                    round(p.market_value / portfolio.equity * 100, 2)
                    if portfolio.equity > 0
                    else 0.0
                ),
            }
            for p in portfolio.positions
        ],
        "open_positions": len([p for p in portfolio.positions if p.shares != 0]),
        "max_open_positions": profile.max_open_positions,
        "gross_exposure_pct": (
            round(portfolio.gross_exposure / portfolio.equity * 100, 2)
            if portfolio.equity > 0
            else 0.0
        ),
        "max_portfolio_exposure_pct": profile.max_portfolio_exposure_pct,
        "sector_exposure": {
            sector: round(value / portfolio.equity * 100, 2) if portfolio.equity > 0 else 0.0
            for sector, value in sorted(by_sector.items(), key=lambda kv: -kv[1])
        },
        "max_sector_pct": profile.max_sector_pct,
        "max_position_pct": profile.max_position_pct,
        "day_pnl": round(portfolio.day_pnl, 2),
        "profile_version": profile.version,
    }
