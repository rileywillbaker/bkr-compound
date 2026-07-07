from sentinel.portfolio.sizing import size_position
from sentinel.risk.profile import RiskProfile

PROFILE = RiskProfile()  # 0.75% risk, 2x ATR stop, 2.0 R:R, 10% max position


def test_basic_sizing_math():
    # equity 100k -> risk $750; ATR 4 -> stop distance 8 -> 93 shares
    # (93 * $100 = $9,300, under the 10% position cap, so pure math applies)
    result = size_position(equity=100_000, entry_price=100.0, atr14=4.0, profile=PROFILE)
    assert result is not None
    assert result.shares == 93
    assert result.stop_distance == 8.0
    assert result.stop_loss == 92.0
    assert result.take_profit == 116.0  # 2R
    assert result.risk_amount == 750.0
    assert result.max_entry_price > 100.0


def test_capped_by_max_position_pct():
    # tiny ATR would give a huge share count; cap at 10% of equity / price
    result = size_position(equity=100_000, entry_price=100.0, atr14=0.05, profile=PROFILE)
    assert result is not None
    assert result.shares == 100  # 10k / 100


def test_zero_when_risk_budget_too_small():
    # stop distance bigger than the whole risk budget -> 0 shares -> None
    result = size_position(equity=1_000, entry_price=100.0, atr14=10.0, profile=PROFILE)
    assert result is None


def test_stop_below_zero_rejected():
    result = size_position(equity=100_000, entry_price=5.0, atr14=3.0, profile=PROFILE)
    assert result is None


def test_invalid_inputs_return_none():
    assert size_position(0, 100, 2, PROFILE) is None
    assert size_position(100_000, 0, 2, PROFILE) is None
    assert size_position(100_000, 100, 0, PROFILE) is None


def test_reward_risk_scales_with_profile():
    profile = RiskProfile(min_reward_risk=3.0)
    result = size_position(equity=100_000, entry_price=100.0, atr14=2.0, profile=profile)
    assert result is not None
    assert result.take_profit == 112.0  # 3R above entry
