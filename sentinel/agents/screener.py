"""Screener Agent (spec §4.2) — deterministic, no LLM.

Scans whatever symbols the run's MarketContext carries — any ticker in the
expanded universe, not just the watchlist — against user-settable filters
(spec §1.6): sector, market cap, price, dollar volume, volatility appetite.
Emits candidates with raw factor scores.
"""

from pydantic import BaseModel, Field

from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.data.context import MarketContext


class ScreenerParams(BaseModel):
    """User-settable universe filters (edited in Settings → Screener)."""

    min_price: float = Field(default=5.0, ge=0)
    min_avg_dollar_volume: float = Field(default=5_000_000, ge=0)
    sectors: list[str] = Field(default_factory=list)  # empty = all sectors
    min_market_cap_millions: float | None = None
    max_market_cap_millions: float | None = None
    max_atr_pct: float | None = None  # volatility appetite; None = use risk profile
    exchanges: list[str] = Field(default_factory=list)  # empty = all


class ScreenResult(BaseModel):
    symbol: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)  # why excluded (empty if eligible)
    momentum_score: float = 0.0  # -100..100 raw factor scores
    trend_score: float = 0.0
    volume_score: float = 0.0


def screen(
    context: MarketContext,
    technicals: dict[str, TechnicalSnapshot],
    params: ScreenerParams,
) -> list[ScreenResult]:
    results: list[ScreenResult] = []
    for symbol, sym_ctx in context.symbols.items():
        snap = technicals.get(symbol)
        reasons: list[str] = []
        if snap is None or snap.bars_used < 60:
            reasons.append("insufficient price history")
            results.append(ScreenResult(symbol=symbol, eligible=False, reasons=reasons))
            continue
        if snap.close < params.min_price:
            reasons.append(f"price {snap.close:.2f} below minimum {params.min_price}")
        adv = snap.avg_dollar_volume20 or 0.0
        if adv < params.min_avg_dollar_volume:
            reasons.append(f"avg dollar volume {adv:,.0f} below minimum")
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
            )
        )
    return results
