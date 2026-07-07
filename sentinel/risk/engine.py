"""Deterministic Risk Engine (spec §5).

Pure functions, zero LLM calls, zero I/O. Evaluates a CandidateOrder against
the active RiskProfile and a PortfolioState snapshot. ALL rules must pass.
There is NO override code path — do not add one.

Missing-data policy: if a value a rule needs is unknown for a BUY, the rule
FAILS (when uncertain, the answer is NO TRADE).
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from sentinel.risk.profile import RiskProfile

Action = Literal["BUY", "SELL", "HOLD", "NO_TRADE"]


class PositionState(BaseModel):
    symbol: str
    shares: int
    price: float  # current mark
    sector: str = ""

    @property
    def market_value(self) -> float:
        return self.shares * self.price


class PortfolioState(BaseModel):
    equity: float  # cash + positions, current
    high_water_mark: float
    day_pnl: float  # realized + unrealized today, dollars (negative = loss)
    positions: list[PositionState] = Field(default_factory=list)

    def position_for(self, symbol: str) -> PositionState | None:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)


class CandidateOrder(BaseModel):
    symbol: str
    action: Action
    shares: int = 0
    entry_price: float = 0.0
    sector: str = ""
    avg_dollar_volume: float | None = None  # 20d average daily dollar volume
    atr_pct: float | None = None  # ATR-14 / price * 100
    trading_days_to_earnings: int | None = None  # None = no earnings scheduled
    correlations: dict[str, float] = Field(default_factory=dict)  # held symbol -> 90d corr

    @property
    def notional(self) -> float:
        return self.shares * self.entry_price


class RuleResult(BaseModel):
    rule: str
    passed: bool
    value: float | None = None
    limit: float | None = None
    detail: str = ""


class RiskCheckResult(BaseModel):
    approved: bool
    symbol: str
    action: Action
    profile_version: int
    checked_at: datetime
    rules: list[RuleResult]

    def failed_rules(self) -> list[str]:
        return [r.rule for r in self.rules if not r.passed]


def _na(rule: str, detail: str) -> RuleResult:
    return RuleResult(rule=rule, passed=True, detail=detail)


def _rule_max_position_pct(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_position_pct"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    existing = p.position_for(c.symbol)
    existing_value = existing.market_value if existing else 0.0
    if p.equity <= 0:
        return RuleResult(rule=rule, passed=False, detail="equity is not positive")
    pct = (existing_value + c.notional) / p.equity * 100
    return RuleResult(
        rule=rule,
        passed=pct <= r.max_position_pct,
        value=round(pct, 4),
        limit=r.max_position_pct,
        detail=f"position would be {pct:.2f}% of equity",
    )


def _rule_max_open_positions(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_open_positions"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    open_now = len([x for x in p.positions if x.shares != 0])
    would_be = open_now if p.position_for(c.symbol) else open_now + 1
    return RuleResult(
        rule=rule,
        passed=would_be <= r.max_open_positions,
        value=would_be,
        limit=r.max_open_positions,
        detail=f"{would_be} open positions after fill",
    )


def _rule_max_daily_loss(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_daily_loss_pct"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action} (halt applies to BUYs)")
    if p.equity <= 0:
        return RuleResult(rule=rule, passed=False, detail="equity is not positive")
    loss_pct = -p.day_pnl / p.equity * 100 if p.day_pnl < 0 else 0.0
    return RuleResult(
        rule=rule,
        passed=loss_pct < r.max_daily_loss_pct,
        value=round(loss_pct, 4),
        limit=r.max_daily_loss_pct,
        detail="daily-loss BUY halt active" if loss_pct >= r.max_daily_loss_pct else "",
    )


def _rule_max_drawdown(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_drawdown_pct"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action} (halt applies to BUYs)")
    if p.high_water_mark <= 0:
        return RuleResult(rule=rule, passed=False, detail="no high-water mark recorded")
    dd_pct = max(0.0, (p.high_water_mark - p.equity) / p.high_water_mark * 100)
    return RuleResult(
        rule=rule,
        passed=dd_pct < r.max_drawdown_pct,
        value=round(dd_pct, 4),
        limit=r.max_drawdown_pct,
        detail="drawdown BUY halt active" if dd_pct >= r.max_drawdown_pct else "",
    )


def _rule_max_sector_pct(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_sector_pct"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    if not c.sector:
        return RuleResult(rule=rule, passed=False, detail="sector unknown for candidate")
    if p.equity <= 0:
        return RuleResult(rule=rule, passed=False, detail="equity is not positive")
    sector_value = sum(
        x.market_value for x in p.positions if x.sector == c.sector and x.symbol != c.symbol
    )
    existing = p.position_for(c.symbol)
    if existing:
        sector_value += existing.market_value
    pct = (sector_value + c.notional) / p.equity * 100
    return RuleResult(
        rule=rule,
        passed=pct <= r.max_sector_pct,
        value=round(pct, 4),
        limit=r.max_sector_pct,
        detail=f"sector '{c.sector}' would be {pct:.2f}% of equity",
    )


def _rule_max_correlated_exposure(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_correlated_exposure"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    if p.equity <= 0:
        return RuleResult(rule=rule, passed=False, detail="equity is not positive")
    held = [x for x in p.positions if x.shares != 0 and x.symbol != c.symbol]
    unknown = [x.symbol for x in held if x.symbol not in c.correlations]
    if unknown:
        return RuleResult(
            rule=rule,
            passed=False,
            detail=f"correlation unknown vs held: {', '.join(sorted(unknown))}",
        )
    correlated_value = sum(
        x.market_value
        for x in held
        if c.correlations.get(x.symbol, 0.0) > r.correlation_threshold
    )
    pct = (correlated_value + c.notional) / p.equity * 100
    return RuleResult(
        rule=rule,
        passed=pct <= r.max_correlated_exposure_pct,
        value=round(pct, 4),
        limit=r.max_correlated_exposure_pct,
        detail=f"correlated (> {r.correlation_threshold:.2f}) exposure incl. candidate",
    )


def _rule_liquidity(c: CandidateOrder, p: PortfolioState, r: RiskProfile) -> RuleResult:
    rule = "min_avg_dollar_volume"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    if c.avg_dollar_volume is None:
        return RuleResult(rule=rule, passed=False, detail="average dollar volume unknown")
    if c.avg_dollar_volume < r.min_avg_dollar_volume:
        return RuleResult(
            rule=rule,
            passed=False,
            value=c.avg_dollar_volume,
            limit=r.min_avg_dollar_volume,
            detail="below liquidity floor",
        )
    participation = (
        c.notional / c.avg_dollar_volume * 100 if c.avg_dollar_volume > 0 else 100.0
    )
    return RuleResult(
        rule=rule,
        passed=participation <= r.max_adv_participation_pct,
        value=round(participation, 4),
        limit=r.max_adv_participation_pct,
        detail=f"order is {participation:.2f}% of ADV",
    )


def _rule_max_atr_pct(c: CandidateOrder, p: PortfolioState, r: RiskProfile) -> RuleResult:
    rule = "max_atr_pct"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    if c.atr_pct is None:
        return RuleResult(rule=rule, passed=False, detail="ATR unknown")
    return RuleResult(
        rule=rule,
        passed=c.atr_pct <= r.max_atr_pct,
        value=round(c.atr_pct, 4),
        limit=r.max_atr_pct,
        detail=f"ATR is {c.atr_pct:.2f}% of price",
    )


def _rule_earnings_blackout(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "earnings_blackout_days"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    if c.trading_days_to_earnings is None:
        return RuleResult(rule=rule, passed=True, detail="no earnings scheduled in window")
    return RuleResult(
        rule=rule,
        passed=c.trading_days_to_earnings > r.earnings_blackout_days,
        value=c.trading_days_to_earnings,
        limit=r.earnings_blackout_days,
        detail=f"earnings in {c.trading_days_to_earnings} trading day(s)",
    )


def _rule_max_portfolio_exposure(
    c: CandidateOrder, p: PortfolioState, r: RiskProfile
) -> RuleResult:
    rule = "max_portfolio_exposure_pct"
    if c.action != "BUY":
        return _na(rule, f"n/a for {c.action}")
    if p.equity <= 0:
        return RuleResult(rule=rule, passed=False, detail="equity is not positive")
    pct = (p.gross_exposure + c.notional) / p.equity * 100
    return RuleResult(
        rule=rule,
        passed=pct <= r.max_portfolio_exposure_pct,
        value=round(pct, 4),
        limit=r.max_portfolio_exposure_pct,
        detail=f"gross exposure would be {pct:.2f}% of equity",
    )


def _rule_order_sanity(c: CandidateOrder, p: PortfolioState, r: RiskProfile) -> RuleResult:
    """BUY/SELL must carry positive shares and price; SELL must not exceed held."""
    rule = "order_sanity"
    if c.action in ("HOLD", "NO_TRADE"):
        return _na(rule, f"n/a for {c.action}")
    if c.shares <= 0 or c.entry_price <= 0:
        return RuleResult(rule=rule, passed=False, detail="shares and price must be positive")
    if c.action == "SELL":
        held = p.position_for(c.symbol)
        if held is None or held.shares < c.shares:
            return RuleResult(
                rule=rule,
                passed=False,
                value=c.shares,
                limit=float(held.shares) if held else 0.0,
                detail="cannot sell more than held (no short selling)",
            )
    return RuleResult(rule=rule, passed=True)


ALL_RULES = [
    _rule_order_sanity,
    _rule_max_position_pct,
    _rule_max_open_positions,
    _rule_max_daily_loss,
    _rule_max_drawdown,
    _rule_max_sector_pct,
    _rule_max_correlated_exposure,
    _rule_liquidity,
    _rule_max_atr_pct,
    _rule_earnings_blackout,
    _rule_max_portfolio_exposure,
]


def evaluate(
    candidate: CandidateOrder, portfolio: PortfolioState, profile: RiskProfile
) -> RiskCheckResult:
    """Evaluate every rule. approved only if ALL pass. Pure function."""
    results = [rule(candidate, portfolio, profile) for rule in ALL_RULES]
    return RiskCheckResult(
        approved=all(r.passed for r in results),
        symbol=candidate.symbol,
        action=candidate.action,
        profile_version=profile.version,
        checked_at=datetime.now(UTC),
        rules=results,
    )
