"""Regime Agent (spec Â§4.1) â€” deterministic classification.

Inputs: SPY daily bars + VIX (FRED VIXCLS). Output: one of
bull-trend / bear-trend / range / high-volatility, with the raw indicator
table so the synthesizer/LLM can cite it.
"""

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel

from sentinel.agents.technicals import adx, bars_to_frame
from sentinel.providers.types import Bar, MacroPoint

RegimeName = Literal["bull-trend", "bear-trend", "range", "high-volatility"]


class RegimeAssessment(BaseModel):
    regime: RegimeName
    spy_close: float | None = None
    spy_sma200: float | None = None
    spy_above_sma200: bool | None = None
    realized_vol20_pctile: float | None = None  # percentile within trailing year
    vix: float | None = None
    adx14: float | None = None
    breadth_available: bool = False
    detail: str = ""


VIX_HIGH = 28.0
VOL_PCTILE_HIGH = 85.0
ADX_TRENDING = 20.0


def classify_regime(
    spy_bars: list[Bar], vix_points: list[MacroPoint] | None = None
) -> RegimeAssessment:
    if len(spy_bars) < 60:
        return RegimeAssessment(
            regime="range", detail="insufficient SPY history; defaulting to range (no-trade bias)"
        )
    df = bars_to_frame(spy_bars)
    close = df["close"]
    last = float(close.iloc[-1])

    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(df) >= 200 else None
    above = last > sma200 if sma200 is not None else None

    returns = close.pct_change().dropna()
    vol20 = returns.rolling(20).std() * np.sqrt(252) * 100
    vol_now = float(vol20.iloc[-1]) if pd.notna(vol20.iloc[-1]) else None
    trailing = vol20.tail(252).dropna()
    pctile = (
        round(float((trailing < vol_now).mean() * 100), 2)
        if vol_now is not None and len(trailing) >= 60
        else None
    )

    vix_value: float | None = None
    if vix_points:
        valued = [p for p in vix_points if p.value is not None]
        if valued:
            vix_value = valued[-1].value

    adx_series = adx(df)
    adx_now = float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else None

    high_vol = (vix_value is not None and vix_value >= VIX_HIGH) or (
        pctile is not None and pctile >= VOL_PCTILE_HIGH
    )
    trending = adx_now is not None and adx_now >= ADX_TRENDING

    if high_vol:
        regime: RegimeName = "high-volatility"
        detail = "volatility elevated (VIX/realized percentile)"
    elif above is True and trending:
        regime, detail = "bull-trend", "SPY above 200-day SMA with ADX trending"
    elif above is False and trending:
        regime, detail = "bear-trend", "SPY below 200-day SMA with ADX trending"
    else:
        regime, detail = "range", "no established trend (ADX weak or SMA200 unavailable)"

    return RegimeAssessment(
        regime=regime,
        spy_close=last,
        spy_sma200=sma200,
        spy_above_sma200=above,
        realized_vol20_pctile=pctile,
        vix=vix_value,
        adx14=round(adx_now, 2) if adx_now is not None else None,
        detail=detail,
    )
