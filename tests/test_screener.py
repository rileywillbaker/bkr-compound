from sentinel.agents.screener import ScreenerParams, screen
from sentinel.agents.technicals import compute_technicals
from tests.synth import make_bars, make_context, make_symbol_context


def _setup(symbol="AAA", **bar_kwargs):
    bars = make_bars(symbol, 250, **bar_kwargs)
    ctx = make_context({symbol: make_symbol_context(symbol, bars=bars)})
    techs = {symbol: compute_technicals(symbol, bars)}
    return ctx, techs


def test_eligible_uptrend_symbol():
    ctx, techs = _setup(drift=0.3)
    [result] = screen(ctx, techs, ScreenerParams())
    assert result.eligible
    assert result.reasons == []
    assert result.trend_score > 0
    assert result.momentum_score > 0


def test_price_below_minimum():
    ctx, techs = _setup(start=3.0, drift=0.0, alternate=0.1)
    [result] = screen(ctx, techs, ScreenerParams(min_price=5.0))
    assert not result.eligible
    assert any("below minimum" in r for r in result.reasons)


def test_dollar_volume_floor():
    ctx, techs = _setup(volume=100)  # ~ $10k/day << $5M default
    [result] = screen(ctx, techs, ScreenerParams())
    assert not result.eligible
    assert any("dollar volume" in r for r in result.reasons)


def test_sector_filter():
    ctx, techs = _setup()  # synth sector is Technology
    [result] = screen(ctx, techs, ScreenerParams(sectors=["Energy"]))
    assert not result.eligible
    assert any("sector" in r for r in result.reasons)
    [result] = screen(ctx, techs, ScreenerParams(sectors=["Technology"]))
    assert result.eligible


def test_market_cap_bounds():
    ctx, techs = _setup()  # cap = 50_000 (millions)
    [result] = screen(ctx, techs, ScreenerParams(min_market_cap_millions=100_000))
    assert any("market cap below" in r for r in result.reasons)
    [result] = screen(ctx, techs, ScreenerParams(max_market_cap_millions=10_000))
    assert any("market cap above" in r for r in result.reasons)
    [result] = screen(
        ctx,
        techs,
        ScreenerParams(min_market_cap_millions=10_000, max_market_cap_millions=100_000),
    )
    assert result.eligible


def test_unknown_market_cap_fails_min_filter():
    bars = make_bars("UNK", 250)
    ctx = make_context({"UNK": make_symbol_context("UNK", bars=bars, market_cap=None)})
    techs = {"UNK": compute_technicals("UNK", bars)}
    [result] = screen(ctx, techs, ScreenerParams(min_market_cap_millions=1_000))
    assert not result.eligible


def test_volatility_appetite():
    ctx, techs = _setup()
    atr_pct = techs["AAA"].atr_pct
    assert atr_pct is not None
    [result] = screen(ctx, techs, ScreenerParams(max_atr_pct=atr_pct / 2))
    assert any("volatility" in r for r in result.reasons)
    [result] = screen(ctx, techs, ScreenerParams(max_atr_pct=atr_pct * 2))
    assert result.eligible


def test_insufficient_history_short_circuits():
    ctx, techs = _setup()
    techs["AAA"] = compute_technicals("AAA", make_bars("AAA", 20))
    [result] = screen(ctx, techs, ScreenerParams())
    assert not result.eligible
    assert result.reasons == ["insufficient price history"]


def test_missing_snapshot():
    ctx, _ = _setup()
    [result] = screen(ctx, {}, ScreenerParams())
    assert not result.eligible
    assert result.reasons == ["insufficient price history"]
