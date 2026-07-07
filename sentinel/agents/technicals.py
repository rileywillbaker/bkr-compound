"""Deterministic technical indicators (spec Â§4.3 Technicals).

All numbers are computed here in pandas; the LLM only ever interprets the
finished snapshot. It never invents numbers.
"""

import numpy as np
import pandas as pd
from pydantic import BaseModel

from sentinel.providers.types import Bar


class TechnicalSnapshot(BaseModel):
    symbol: str
    close: float
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None  # ATR/close * 100
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    above_sma20: bool | None = None
    above_sma50: bool | None = None
    above_sma200: bool | None = None
    vwap20: float | None = None
    relative_volume: float | None = None  # today vs 20d average
    pct_from_52w_high: float | None = None
    pct_from_52w_low: float | None = None
    support: float | None = None  # nearest swing low below close
    resistance: float | None = None  # nearest swing high above close
    avg_dollar_volume20: float | None = None
    realized_vol20_annualized: float | None = None
    adx14: float | None = None
    bars_used: int = 0


def bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [b.volume for b in bars],
        }
    ).sort_values("ts")
    return df.reset_index(drop=True)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss.notna(), np.nan)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, min_periods=slow).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, min_periods=signal).mean()
    return line, sig, line - sig


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / period, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, min_periods=period).mean()


def swing_levels(df: pd.DataFrame, window: int = 5) -> tuple[list[float], list[float]]:
    """Swing highs/lows: local extremes over +/- window bars."""
    highs, lows = [], []
    h, lo = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(window, len(df) - window):
        if h[i] == max(h[i - window : i + window + 1]):
            highs.append(float(h[i]))
        if lo[i] == min(lo[i - window : i + window + 1]):
            lows.append(float(lo[i]))
    return highs, lows


def compute_technicals(symbol: str, bars: list[Bar]) -> TechnicalSnapshot:
    if len(bars) < 2:
        return TechnicalSnapshot(symbol=symbol, close=float(bars[-1].close) if bars else 0.0)
    df = bars_to_frame(bars)
    close = df["close"]
    last = float(close.iloc[-1])
    n = len(df)

    snap = TechnicalSnapshot(symbol=symbol, close=last, bars_used=n)

    def _val(series: pd.Series) -> float | None:
        v = series.iloc[-1]
        return float(v) if pd.notna(v) else None

    if n >= 15:
        snap.rsi14 = _val(rsi(close))
        atr_series = atr(df)
        snap.atr14 = _val(atr_series)
        if snap.atr14 is not None and last > 0:
            snap.atr_pct = round(snap.atr14 / last * 100, 4)
        snap.adx14 = _val(adx(df))
    if n >= 35:
        line, sig, hist = macd(close)
        snap.macd, snap.macd_signal, snap.macd_hist = _val(line), _val(sig), _val(hist)
    for period, attr in ((20, "sma20"), (50, "sma50"), (200, "sma200")):
        if n >= period:
            value = float(close.rolling(period).mean().iloc[-1])
            setattr(snap, attr, value)
            setattr(snap, f"above_{attr}", last > value)
    if n >= 20:
        tail = df.tail(20)
        vol_sum = tail["volume"].sum()
        if vol_sum > 0:
            typical = (tail["high"] + tail["low"] + tail["close"]) / 3
            snap.vwap20 = float((typical * tail["volume"]).sum() / vol_sum)
        avg_vol = tail["volume"].iloc[:-1].mean() if n > 20 else tail["volume"].mean()
        if avg_vol and avg_vol > 0:
            snap.relative_volume = round(float(df["volume"].iloc[-1] / avg_vol), 4)
        snap.avg_dollar_volume20 = float((tail["close"] * tail["volume"]).mean())
        returns = close.pct_change().tail(20)
        snap.realized_vol20_annualized = round(float(returns.std() * np.sqrt(252) * 100), 4)

    year = df.tail(252)
    hi, lo = float(year["high"].max()), float(year["low"].min())
    if hi > 0:
        snap.pct_from_52w_high = round((last - hi) / hi * 100, 4)
    if lo > 0:
        snap.pct_from_52w_low = round((last - lo) / lo * 100, 4)

    swing_highs, swing_lows = swing_levels(df)
    below = [s for s in swing_lows if s < last]
    above = [s for s in swing_highs if s > last]
    snap.support = max(below) if below else None
    snap.resistance = min(above) if above else None
    return snap
