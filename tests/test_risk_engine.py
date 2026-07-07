"""Exhaustive risk-engine tests (spec: every rule pass, fail, boundary).

The engine is pure: all cases are constructed literally, no I/O.
"""

import pytest

from sentinel.risk.engine import (
    CandidateOrder,
    PortfolioState,
    PositionState,
    RiskCheckResult,
    evaluate,
)
from sentinel.risk.profile import RiskProfile

PROFILE = RiskProfile()  # spec defaults: 10% pos, 8 open, 2% daily, 10% dd, 25% sector,
# 30% corr, $5M ADV / 1% part., 8% ATR, 2d blackout, 100% gross


def portfolio(**kw) -> PortfolioState:
    base = dict(equity=100_000.0, high_water_mark=100_000.0, day_pnl=0.0, positions=[])
    base.update(kw)
    return PortfolioState(**base)


def candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA",
        action="BUY",
        shares=50,
        entry_price=100.0,  # $5,000 notional = 5% of equity
        sector="Semiconductors",
        avg_dollar_volume=50_000_000.0,
        atr_pct=3.0,
        trading_days_to_earnings=None,
        correlations={},
    )
    base.update(kw)
    return CandidateOrder(**base)


def rule(result: RiskCheckResult, name: str):
    return next(r for r in result.rules if r.rule == name)


# ------------------------------------------------------------- approval ----
def test_clean_buy_is_approved():
    result = evaluate(candidate(), portfolio(), PROFILE)
    assert result.approved
    assert result.failed_rules() == []
    assert len(result.rules) == 11  # every rule evaluated and reported


def test_no_trade_and_hold_pass_everything():
    for action in ("NO_TRADE", "HOLD"):
        result = evaluate(candidate(action=action, shares=0, entry_price=0), portfolio(), PROFILE)
        assert result.approved, action


def test_single_failure_vetoes():
    result = evaluate(candidate(atr_pct=99.0), portfolio(), PROFILE)
    assert not result.approved
    assert result.failed_rules() == ["max_atr_pct"]


# --------------------------------------------------------- order_sanity ----
def test_sanity_zero_shares_fails():
    result = evaluate(candidate(shares=0), portfolio(), PROFILE)
    assert "order_sanity" in result.failed_rules()


def test_sanity_zero_price_fails():
    result = evaluate(candidate(entry_price=0), portfolio(), PROFILE)
    assert "order_sanity" in result.failed_rules()


def test_sanity_sell_more_than_held_fails():
    held = portfolio(positions=[PositionState(symbol="NVDA", shares=10, price=100.0)])
    result = evaluate(candidate(action="SELL", shares=11), held, PROFILE)
    assert "order_sanity" in result.failed_rules()


def test_sanity_sell_within_held_passes():
    held = portfolio(positions=[PositionState(symbol="NVDA", shares=10, price=100.0)])
    result = evaluate(candidate(action="SELL", shares=10), held, PROFILE)
    assert result.approved


def test_sanity_sell_unheld_fails():
    result = evaluate(candidate(action="SELL", shares=1), portfolio(), PROFILE)
    assert "order_sanity" in result.failed_rules()


# ---------------------------------------------------- max_position_pct ----
def test_position_pct_boundary_exact_limit_passes():
    # 10% of 100k = 10k = 100 shares @ 100
    result = evaluate(candidate(shares=100), portfolio(), PROFILE)
    assert rule(result, "max_position_pct").passed


def test_position_pct_over_limit_fails():
    result = evaluate(candidate(shares=101), portfolio(), PROFILE)
    assert "max_position_pct" in result.failed_rules()


def test_position_pct_counts_existing_position():
    held = portfolio(positions=[PositionState(symbol="NVDA", shares=60, price=100.0)])
    # existing 6% + new 5% = 11% > 10%
    result = evaluate(candidate(shares=50), held, PROFILE)
    assert "max_position_pct" in result.failed_rules()


def test_position_pct_nonpositive_equity_fails():
    result = evaluate(candidate(), portfolio(equity=0.0), PROFILE)
    assert "max_position_pct" in result.failed_rules()


# --------------------------------------------------- max_open_positions ----
def _positions(n: int) -> list[PositionState]:
    return [PositionState(symbol=f"S{i}", shares=10, price=10.0) for i in range(n)]


def test_open_positions_boundary_passes():
    result = evaluate(candidate(), portfolio(positions=_positions(7)), PROFILE)
    assert rule(result, "max_open_positions").passed  # 8th position allowed


def test_open_positions_over_limit_fails():
    result = evaluate(candidate(), portfolio(positions=_positions(8)), PROFILE)
    assert "max_open_positions" in result.failed_rules()


def test_open_positions_adding_to_existing_passes():
    positions = _positions(7) + [PositionState(symbol="NVDA", shares=5, price=100.0)]
    result = evaluate(candidate(shares=10), portfolio(positions=positions), PROFILE)
    assert rule(result, "max_open_positions").passed  # still 8 symbols


# ----------------------------------------------------- max_daily_loss ----
def test_daily_loss_below_limit_passes():
    result = evaluate(candidate(), portfolio(day_pnl=-1_999.0), PROFILE)
    assert rule(result, "max_daily_loss_pct").passed


def test_daily_loss_at_limit_halts_buys():
    result = evaluate(candidate(), portfolio(day_pnl=-2_000.0), PROFILE)
    assert "max_daily_loss_pct" in result.failed_rules()


def test_daily_gain_passes():
    result = evaluate(candidate(), portfolio(day_pnl=5_000.0), PROFILE)
    assert rule(result, "max_daily_loss_pct").passed


def test_daily_loss_halt_does_not_block_sells():
    held = portfolio(
        day_pnl=-5_000.0,
        positions=[PositionState(symbol="NVDA", shares=50, price=100.0)],
    )
    result = evaluate(candidate(action="SELL", shares=50), held, PROFILE)
    assert result.approved


# ------------------------------------------------------- max_drawdown ----
def test_drawdown_below_limit_passes():
    result = evaluate(candidate(), portfolio(equity=90_001.0), PROFILE)
    assert rule(result, "max_drawdown_pct").passed


def test_drawdown_at_limit_halts_buys():
    result = evaluate(candidate(), portfolio(equity=90_000.0), PROFILE)
    assert "max_drawdown_pct" in result.failed_rules()


def test_drawdown_missing_hwm_fails():
    result = evaluate(candidate(), portfolio(high_water_mark=0.0), PROFILE)
    assert "max_drawdown_pct" in result.failed_rules()


# ------------------------------------------------------ max_sector_pct ----
def test_sector_boundary_exact_limit_passes():
    held = portfolio(
        positions=[PositionState(symbol="AMD", shares=200, price=100.0, sector="Semiconductors")]
    )  # 20% sector + 5% new = 25% exactly
    result = evaluate(candidate(), held, PROFILE)
    assert rule(result, "max_sector_pct").passed


def test_sector_over_limit_fails():
    held = portfolio(
        positions=[PositionState(symbol="AMD", shares=201, price=100.0, sector="Semiconductors")]
    )
    result = evaluate(candidate(), held, PROFILE)
    assert "max_sector_pct" in result.failed_rules()


def test_sector_other_sectors_ignored():
    held = portfolio(
        positions=[PositionState(symbol="XOM", shares=500, price=100.0, sector="Energy")]
    )
    result = evaluate(candidate(), held, PROFILE)
    assert rule(result, "max_sector_pct").passed


def test_sector_unknown_fails_conservative():
    result = evaluate(candidate(sector=""), portfolio(), PROFILE)
    assert "max_sector_pct" in result.failed_rules()


# --------------------------------------------- max_correlated_exposure ----
def test_correlated_exposure_counts_high_corr_only():
    held = portfolio(
        positions=[
            PositionState(symbol="AMD", shares=200, price=100.0),  # 20%, corr .9
            PositionState(symbol="XOM", shares=200, price=100.0),  # 20%, corr .1
        ]
    )
    # correlated: 20% + candidate 5% = 25% <= 30%
    result = evaluate(
        candidate(correlations={"AMD": 0.9, "XOM": 0.1}), held, PROFILE
    )
    assert rule(result, "max_correlated_exposure").passed


def test_correlated_exposure_over_limit_fails():
    held = portfolio(
        positions=[PositionState(symbol="AMD", shares=260, price=100.0)]  # 26%
    )
    result = evaluate(candidate(correlations={"AMD": 0.95}), held, PROFILE)
    # 26% + 5% = 31% > 30%
    assert "max_correlated_exposure" in result.failed_rules()


def test_correlated_exposure_boundary_at_threshold_not_counted():
    held = portfolio(
        positions=[PositionState(symbol="AMD", shares=400, price=100.0)]  # 40%
    )
    # corr exactly 0.7 is NOT > 0.7, so not counted; only candidate 5% counts
    result = evaluate(candidate(correlations={"AMD": 0.7}), held, PROFILE)
    assert rule(result, "max_correlated_exposure").passed


def test_correlated_exposure_missing_correlation_fails():
    held = portfolio(positions=[PositionState(symbol="AMD", shares=10, price=100.0)])
    result = evaluate(candidate(correlations={}), held, PROFILE)
    assert "max_correlated_exposure" in result.failed_rules()


# ------------------------------------------------------------ liquidity ----
def test_liquidity_below_floor_fails():
    result = evaluate(candidate(avg_dollar_volume=4_999_999.0), portfolio(), PROFILE)
    assert "min_avg_dollar_volume" in result.failed_rules()


def test_liquidity_at_floor_passes():
    result = evaluate(candidate(avg_dollar_volume=5_000_000.0), portfolio(), PROFILE)
    assert rule(result, "min_avg_dollar_volume").passed


def test_liquidity_participation_over_1pct_fails():
    # $5,000 notional / $400,000 ADV = 1.25% > 1%  — but ADV below floor too;
    # use ADV above floor: $5M ADV, notional $60k -> 1.2%
    result = evaluate(
        candidate(shares=600, entry_price=100.0, avg_dollar_volume=5_000_000.0),
        portfolio(equity=10_000_000.0),
        PROFILE,
    )
    assert "min_avg_dollar_volume" in result.failed_rules()


def test_liquidity_participation_boundary_passes():
    # notional 50k of 5M ADV = exactly 1%
    result = evaluate(
        candidate(shares=500, entry_price=100.0, avg_dollar_volume=5_000_000.0),
        portfolio(equity=10_000_000.0),
        PROFILE,
    )
    assert rule(result, "min_avg_dollar_volume").passed


def test_liquidity_unknown_fails_conservative():
    result = evaluate(candidate(avg_dollar_volume=None), portfolio(), PROFILE)
    assert "min_avg_dollar_volume" in result.failed_rules()


# ---------------------------------------------------------- max_atr_pct ----
def test_atr_boundary_at_limit_passes():
    result = evaluate(candidate(atr_pct=8.0), portfolio(), PROFILE)
    assert rule(result, "max_atr_pct").passed


def test_atr_over_limit_fails():
    result = evaluate(candidate(atr_pct=8.01), portfolio(), PROFILE)
    assert "max_atr_pct" in result.failed_rules()


def test_atr_unknown_fails_conservative():
    result = evaluate(candidate(atr_pct=None), portfolio(), PROFILE)
    assert "max_atr_pct" in result.failed_rules()


# ------------------------------------------------ earnings_blackout_days ----
def test_earnings_none_scheduled_passes():
    result = evaluate(candidate(trading_days_to_earnings=None), portfolio(), PROFILE)
    assert rule(result, "earnings_blackout_days").passed


def test_earnings_outside_blackout_passes():
    result = evaluate(candidate(trading_days_to_earnings=3), portfolio(), PROFILE)
    assert rule(result, "earnings_blackout_days").passed


def test_earnings_boundary_at_blackout_fails():
    result = evaluate(candidate(trading_days_to_earnings=2), portfolio(), PROFILE)
    assert "earnings_blackout_days" in result.failed_rules()


def test_earnings_today_fails():
    result = evaluate(candidate(trading_days_to_earnings=0), portfolio(), PROFILE)
    assert "earnings_blackout_days" in result.failed_rules()


# -------------------------------------------- max_portfolio_exposure_pct ----
def test_gross_exposure_boundary_passes():
    held = portfolio(
        positions=[PositionState(symbol="AAA", shares=950, price=100.0, sector="Energy")]
    )  # 95% + 5% new = 100% exactly
    result = evaluate(candidate(correlations={"AAA": 0.0}), held, PROFILE)
    assert rule(result, "max_portfolio_exposure_pct").passed


def test_gross_exposure_over_limit_fails():
    held = portfolio(
        positions=[PositionState(symbol="AAA", shares=960, price=100.0, sector="Energy")]
    )
    result = evaluate(candidate(correlations={"AAA": 0.0}), held, PROFILE)
    assert "max_portfolio_exposure_pct" in result.failed_rules()


# ----------------------------------------------------------- audit trail ----
def test_every_rule_reports_pass_fail_and_values():
    result = evaluate(candidate(shares=101, atr_pct=9.0), portfolio(), PROFILE)
    assert not result.approved
    names = {r.rule for r in result.rules}
    assert names == {
        "order_sanity",
        "max_position_pct",
        "max_open_positions",
        "max_daily_loss_pct",
        "max_drawdown_pct",
        "max_sector_pct",
        "max_correlated_exposure",
        "min_avg_dollar_volume",
        "max_atr_pct",
        "earnings_blackout_days",
        "max_portfolio_exposure_pct",
    }
    pos_rule = rule(result, "max_position_pct")
    assert pos_rule.value is not None and pos_rule.limit == 10.0
    assert result.profile_version == PROFILE.version


def test_no_override_code_path_exists():
    """Spec §5: 'There is no override code path — do not build one.'"""
    import sentinel.risk.engine as engine_module

    banned = [n for n in dir(engine_module) if "override" in n.lower() or "force" in n.lower()]
    assert banned == []
    # RiskCheckResult.approved is derived solely from rule outcomes
    result = evaluate(candidate(atr_pct=None), portfolio(), PROFILE)
    assert result.approved is False


@pytest.mark.parametrize("field", ["max_position_pct", "max_daily_loss_pct", "max_atr_pct"])
def test_profile_rejects_nonsense_limits(field):
    with pytest.raises(ValueError):
        RiskProfile(**{field: 0})
    with pytest.raises(ValueError):
        RiskProfile(**{field: -1})
