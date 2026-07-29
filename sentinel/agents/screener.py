"""Screener Agent (spec §4.2) — deterministic, no LLM, unlimited.

This is the widest and cheapest stage of the pipeline: it sweeps every symbol
in the run's MarketContext (the full expanded universe, not the watchlist) and
applies three families of filters in order of how cheap they are to fail:

  1. data quality   — enough price history to compute what we rely on
  2. technical      — price, volatility appetite
  3. liquidity      — average dollar volume (can you actually get filled?)
  4. fundamental    — market cap floor, sector known, optional valuation cap

The defaults deliberately exclude penny stocks, OTC names, micro caps, and
symbols with thin or missing data — the prompt-level rule that B-Quant hunts
for quality, not lottery tickets. Everything here is pure arithmetic over data
already in the database, so widening the universe costs nothing but CPU.

Emits raw factor scores (momentum / trend / volume / relative strength) that
downstream strategy fitting and the LLM-gating stage rank against.
"""

from pydantic import BaseModel, Field

from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.data.context import MarketContext
from sentinel.providers.types import Bar

# Trading days used for the relative-strength comparison vs the benchmark.
RS_LOOKBACK = 63  # ~one quarter


class ScreenerParams(BaseModel):
    """User-settable universe filters (edited in Settings → Screener).

    Quality floors default ON: a large, liquid, well-covered universe produces
    better expectancy than a wide one full of illiquid names, and it costs the
    same to screen.
    """

    # --- data quality ---
    min_bars: int = Field(default=200, ge=2)  # a full year: SMA200 must exist
    require_sector: bool = True  # unknown sector = poor coverage (and a risk veto)

    # --- technical / volatility ---
    min_price: float = Field(default=5.0, ge=0)  # excludes penny stocks
    max_atr_pct: float | None = None  # volatility appetite; None = use risk profile

    # --- liquidity ---
    min_avg_dollar_volume: float = Field(default=5_000_000, ge=0)

    # --- fundamental ---
    min_market_cap_millions: float | None = 2_000.0  # $2B floor: no micro caps
    max_market_cap_millions: float | None = None
    max_pe: float | None = None  # optional valuation ceiling; None = off
    sectors: list[str] = Field(default_factory=list)  # empty = all sectors
    exchanges: list[str] = Field(default_factory=list)  # empty = all


class ScreenResult(BaseModel):
    symbol: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)  # why excluded (empty if eligible)
    momentum_score: float = 0.0  # -100..100 raw factor scores
    trend_score: float = 0.0
    volume_score: float = 0.0
    rs_score: float = 0.0  # relative strength vs the benchmark, -100..100

    @property
    def composite_score(self) -> float:
        """Single deterministic ranking number used to prioritise work."""
        return round(
            0.35 * self.trend_score
            + 0.30 * self.momentum_score
            + 0.25 * self.rs_score
            + 0.10 * self.volume_score,
            2,
        )


def _pct_return(bars: list[Bar], lookback: int) -> float | None:
    """Simple return over the last `lookback` bars, or None without history."""
    if len(bars) <= lookback:
        return None
    start = float(bars[-1 - lookback].close)
    end = float(bars[-1].close)
    if start <= 0:
        return None
    return (end / start - 1) * 100


def relative_strength(
    symbol_bars: list[Bar], benchmark_bars: list[Bar], lookback: int = RS_LOOKBACK
) -> float:
    """Excess return vs the benchmark over the lookback, clamped to -100..100.

    Relative strength is the single most reliable free screen there is, and it
    costs one subtraction over bars already stored.
    """
    sym = _pct_return(symbol_bars, lookback)
    bench = _pct_return(benchmark_bars, lookback)
    if sym is None or bench is None:
        return 0.0
    return round(max(-100.0, min(100.0, sym - bench)), 2)


def screen(
    context: MarketContext,
    technicals: dict[str, TechnicalSnapshot],
    params: ScreenerParams,
) -> list[ScreenResult]:
    results: list[ScreenResult] = []
    for symbol, sym_ctx in context.symbols.items():
        snap = technicals.get(symbol)
        reasons: list[str] = []
        if snap is None or snap.bars_used < params.min_bars:
            reasons.append("insufficient price history")
            results.append(ScreenResult(symbol=symbol, eligible=False, reasons=reasons))
            continue
        if snap.close < params.min_price:
            reasons.append(f"price {snap.close:.2f} below minimum {params.min_price}")
        adv = snap.avg_dollar_volume20 or 0.0
        if adv < params.min_avg_dollar_volume:
            reasons.append(f"avg dollar volume {adv:,.0f} below minimum")
        if params.require_sector and not sym_ctx.sector:
            reasons.append("sector unknown (insufficient data coverage)")
        if params.sectors and sym_ctx.sector not in params.sectors:
            reasons.append(f"sector '{sym_ctx.sector}' not selected")
        if params.exchanges:
            pass  # exchange stored in fundamentals; enforced when available
        cap = sym_ctx.market_cap
        if params.min_market_cap_millions is not None and (
            cap is None or cap < params.min_market_cap_millions
        ):
            reasons.append("market cap below minimum (or unknown)")
        if params.max_market_cap_millions is not None and cap is not None and (
            cap > params.max_market_cap_millions
        ):
            reasons.append("market cap above maximum")
        if params.max_pe is not None and sym_ctx.pe is not None and sym_ctx.pe > params.max_pe:
            reasons.append(f"P/E {sym_ctx.pe:.1f} above maximum {params.max_pe}")
        if params.max_atr_pct is not None and (
            snap.atr_pct is None or snap.atr_pct > params.max_atr_pct
        ):
            reasons.append("volatility above appetite (or unknown)")

        momentum = 0.0
        if snap.rsi14 is not None:
            momentum += (snap.rsi14 - 50) * 1.2  # -60..+60
        if snap.macd_hist is not None and snap.close > 0:
            momentum += max(-40, min(40, snap.macd_hist / snap.close * 4000))
        trend = 0.0
        for flag in (snap.above_sma20, snap.above_sma50, snap.above_sma200):
            if flag is True:
                trend += 33.4
            elif flag is False:
                trend -= 33.4
        volume = 0.0
        if snap.relative_volume is not None:
            volume = max(-100, min(100, (snap.relative_volume - 1.0) * 100))

        results.append(
            ScreenResult(
                symbol=symbol,
                eligible=not reasons,
                reasons=reasons,
                momentum_score=round(max(-100, min(100, momentum)), 2),
                trend_score=round(max(-100, min(100, trend)), 2),
                volume_score=round(volume, 2),
                rs_score=relative_strength(sym_ctx.daily_bars, context.spy_bars),
            )
        )
    return results
