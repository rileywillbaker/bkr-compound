from sentinel.agents.technicals import compute_technicals
from tests.synth import make_bars


def test_uptrend_snapshot():
    snap = compute_technicals("UP", make_bars("UP", 250, start=100.0, drift=0.3))
    assert snap.bars_used == 250
    assert snap.close == 100.0 + 0.3 * 249
    assert snap.above_sma20 and snap.above_sma50 and snap.above_sma200
    assert snap.rsi14 is not None and snap.rsi14 > 50
    assert snap.macd_hist is not None
    assert snap.atr14 is not None and snap.atr14 > 0
    assert snap.atr_pct is not None and snap.atr_pct > 0
    assert snap.adx14 is not None
    # last close is the highest close; the 52w high (bar high) sits just above it
    assert snap.pct_from_52w_high is not None and snap.pct_from_52w_high <= 0
    assert snap.pct_from_52w_low is not None and snap.pct_from_52w_low > 0
    assert snap.avg_dollar_volume20 is not None and snap.avg_dollar_volume20 > 0


def test_downtrend_flags():
    snap = compute_technicals("DN", make_bars("DN", 250, start=200.0, drift=-0.3))
    assert snap.above_sma20 is False
    assert snap.above_sma50 is False
    assert snap.above_sma200 is False
    assert snap.rsi14 is not None and snap.rsi14 < 50


def test_short_history_leaves_longer_indicators_none():
    snap = compute_technicals("SH", make_bars("SH", 30))
    assert snap.bars_used == 30
    assert snap.rsi14 is not None  # needs 15
    assert snap.atr14 is not None
    assert snap.macd is None  # needs 35
    assert snap.sma20 is not None
    assert snap.sma50 is None
    assert snap.sma200 is None
    assert snap.above_sma200 is None


def test_single_bar_returns_minimal_snapshot():
    snap = compute_technicals("ONE", make_bars("ONE", 1))
    assert snap.bars_used == 0
    assert snap.close == 100.0
    assert snap.rsi14 is None


def test_no_bars():
    snap = compute_technicals("NONE", [])
    assert snap.close == 0.0
    assert snap.bars_used == 0


def test_relative_volume_spike():
    bars = make_bars("RV", 100, volume=1_000_000, last_volume=3_000_000)
    snap = compute_technicals("RV", bars)
    assert snap.relative_volume is not None
    assert abs(snap.relative_volume - 3.0) < 0.01


def test_support_resistance_in_range_series():
    # zigzag around a flat price: swing lows below close, swing highs above
    snap = compute_technicals("RG", make_bars("RG", 120, drift=0.0, alternate=2.0))
    assert snap.support is not None and snap.support < snap.close
    assert snap.resistance is not None and snap.resistance > snap.close
