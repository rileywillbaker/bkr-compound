"""Portfolio manager: deterministic hold/trim/tighten/add/exit verdicts.

No LLM is involved anywhere here — these tests assert the arithmetic and, most
importantly, that anything ADDING exposure still passes the risk engine.
"""

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.db.models import Position
from sentinel.portfolio.manager import review_position, review_positions
from sentinel.risk.engine import PortfolioState, PositionState
from sentinel.risk.profile import RiskProfile

PROFILE = RiskProfile()
BULL = RegimeAssessment(regime="bull-trend", confidence=0.8, detail="synthetic")
CHOP = RegimeAssessment(regime="high-volatility", confidence=0.8, detail="synthetic")


def snap(**kw) -> TechnicalSnapshot:
    base = dict(
        symbol="NVDA",
        close=100.0,
        atr14=2.0,  # stop distance = 2 x ATR = 4.0 at the default multiple
        atr_pct=2.0,
        above_sma20=True,
        above_sma50=True,
        above_sma200=True,
        avg_dollar_volume20=50_000_000.0,
        bars_used=250,
    )
    base.update(kw)
    return TechnicalSnapshot(**base)


def portfolio(equity=100_000.0, positions=None) -> PortfolioState:
    return PortfolioState(
        equity=equity,
        high_water_mark=equity,
        day_pnl=0.0,
        positions=positions or [],
    )


def review(db, *, mark, cost, shares=10, snapshot=None, regime=BULL, equity=100_000.0, **kw):
    return review_position(
        db,
        symbol="NVDA",
        shares=shares,
        cost_basis=cost,
        mark=mark,
        sector="Technology",
        snap=snapshot or snap(close=mark),
        portfolio=portfolio(equity, [PositionState(symbol="NVDA", shares=shares, price=mark)]),
        profile=PROFILE,
        regime=regime,
        **kw,
    )


def test_stop_breach_is_an_exit(db):
    # cost 100, stop distance 4.0 → a mark of 96 is -1R
    result = review(db, mark=95.0, cost=100.0, snapshot=snap(close=95.0))
    assert result.action == "EXIT"
    assert result.shares_delta == -10
    assert result.urgency == 5
    assert "stop level breached" in result.reasons[0]


def test_broken_trend_is_an_exit(db):
    result = review(
        db,
        mark=99.0,
        cost=100.0,
        snapshot=snap(close=99.0, above_sma50=False, above_sma200=False),
    )
    assert result.action == "EXIT"
    assert "trend broken" in result.reasons[0]


def test_target_reached_takes_partial_profits(db):
    # +2R at the default 2.0 reward:risk → bank half, let the rest run
    result = review(db, mark=108.0, cost=100.0, snapshot=snap(close=108.0))
    assert result.action == "TAKE_PARTIAL_PROFITS"
    assert result.shares_delta == -5
    assert result.r_multiple == 2.0


def test_target_reached_but_momentum_rolling_over_is_a_full_exit(db):
    result = review(
        db, mark=108.0, cost=100.0, snapshot=snap(close=108.0, above_sma20=False)
    )
    assert result.action == "EXIT"
    assert result.shares_delta == -10


def test_one_r_onside_tightens_the_stop_to_at_least_breakeven(db):
    result = review(db, mark=104.5, cost=100.0, snapshot=snap(close=104.5))
    assert result.action == "TIGHTEN_STOP"
    assert result.shares_delta == 0
    assert result.suggested_stop is not None and result.suggested_stop >= 100.0


def test_oversized_position_is_trimmed_to_the_limit(db):
    """A 30% position against a 10% cap must come down — the size limit is a
    hard risk-profile rule, not a preference."""
    result = review(db, mark=101.0, cost=100.0, shares=300, equity=100_000.0)
    assert result.action == "REDUCE"
    assert result.shares_delta < 0
    assert result.shares + result.shares_delta <= 100  # 10% of 100k at $101


def test_working_position_with_headroom_can_be_increased(db):
    result = review(db, mark=102.0, cost=100.0, shares=10, equity=100_000.0)
    assert result.action == "INCREASE"
    assert result.shares_delta > 0
    # the add went through the real risk engine, not a shortcut
    assert result.risk_check is not None and result.risk_check.approved


def test_increase_is_suppressed_in_a_high_volatility_regime(db):
    result = review(db, mark=102.0, cost=100.0, shares=10, regime=CHOP)
    assert result.action == "HOLD"
    assert any("high-volatility" in r for r in result.reasons)


def test_increase_is_suppressed_inside_the_earnings_blackout(db):
    result = review(db, mark=102.0, cost=100.0, shares=10, trading_days_to_earnings=1)
    assert result.action == "HOLD"
    assert any("earnings in 1" in r for r in result.reasons)


def test_quiet_position_is_a_hold(db):
    result = review(
        db, mark=100.5, cost=100.0, shares=10, snapshot=snap(close=100.5, above_sma20=False)
    )
    assert result.action == "HOLD"
    assert result.shares_delta == 0


def test_missing_history_is_no_action_never_a_trade(db):
    result = review(db, mark=100.0, cost=100.0, snapshot=snap(atr14=None))
    assert result.action == "NO_ACTION"
    assert result.shares_delta == 0
    assert "not enough price history" in result.reasons[0]


def test_review_positions_reads_holdings_and_sorts_by_urgency(db):
    db.add(Position(symbol="AAA", shares=10, cost_basis=100))
    db.add(Position(symbol="BBB", shares=0, cost_basis=50))  # closed → ignored
    db.flush()

    reviews = review_positions(db)
    assert [r.symbol for r in reviews] == ["AAA"]
    assert reviews[0].urgency >= 1


def test_no_positions_means_no_reviews(db):
    assert review_positions(db) == []
