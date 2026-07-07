from sentinel.agents.regime import classify_regime
from tests.synth import make_bars, make_vix


def test_insufficient_history_defaults_to_range():
    result = classify_regime(make_bars("SPY", 30))
    assert result.regime == "range"
    assert "insufficient" in result.detail


def test_bull_trend():
    result = classify_regime(make_bars("SPY", 250, start=400.0, drift=0.5), make_vix(14.0))
    assert result.regime == "bull-trend"
    assert result.spy_above_sma200 is True
    assert result.adx14 is not None and result.adx14 >= 20
    assert result.vix == 14.0


def test_bear_trend():
    # noisy decline for a year, then a calm final month: still a downtrend,
    # and recent realized vol sits low in its trailing-year distribution
    noisy = make_bars("SPY", 220, start=600.0, growth=0.999, alternate=2.0)
    calm = make_bars(
        "SPY", 30, start=600.0 * 0.999**220, growth=0.999, start_day=220
    )
    result = classify_regime(noisy + calm, make_vix(20.0))
    assert result.regime == "bear-trend"
    assert result.spy_above_sma200 is False


def test_high_volatility_via_vix_overrides_trend():
    result = classify_regime(make_bars("SPY", 250, start=400.0, drift=0.5), make_vix(35.0))
    assert result.regime == "high-volatility"


def test_range_when_no_trend():
    # flat zigzag: ADX stays weak, price hugs the 200-day SMA
    result = classify_regime(make_bars("SPY", 250, start=400.0, drift=0.0, alternate=1.0))
    assert result.regime == "range"
