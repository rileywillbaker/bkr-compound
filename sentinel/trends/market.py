"""Market confirmation for a theme, computed from bars already in the database.

This is the module that keeps the agent honest. News volume, government
activity and social chatter all measure *talk*; the functions here measure
whether money actually moved. A theme with loud coverage and no market
confirmation is the exact shape of a hype cycle, and `scoring.py` needs to be
able to see the difference.

Everything is arithmetic over `bars` — one query per run, no provider calls,
no cost. The ETF "flow" measures are explicitly proxies (real creation and
redemption data is a paid product) and are named as proxies wherever they
surface to the user.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import BarRow

log = structlog.get_logger()

BENCHMARK = "SPY"

Series = dict[str, list[tuple[float, int]]]  # symbol -> [(close, volume)] ascending


class BasketPerformance(BaseModel):
    """How a theme's constituents behaved as a group."""

    symbols_with_data: int = 0
    return_21d_pct: float | None = None
    return_63d_pct: float | None = None
    excess_21d_pct: float | None = None  # vs the benchmark
    excess_63d_pct: float | None = None
    breadth_pct: float | None = None  # % of members above their own 50-day
    participation_pct: float | None = None  # % that outperformed the benchmark
    median_return_21d_pct: float | None = None
    # The single best performer's share of the basket's total move. A theme
    # carried entirely by one name is a single-stock story wearing a costume.
    concentration_pct: float | None = None


class EtfActivity(BaseModel):
    """Free proxy for thematic fund flows, from the ETF's own price/volume."""

    etf: str
    return_21d_pct: float | None = None
    excess_21d_pct: float | None = None
    dollar_volume_trend_pct: float | None = None  # last 5d vs prior 20d
    above_sma50: bool | None = None
    above_sma200: bool | None = None
    avg_dollar_volume20: float | None = None

    @property
    def accumulating(self) -> bool:
        """Rising on rising turnover, and outperforming — as close as free
        data gets to 'money is going in'."""
        return (
            (self.excess_21d_pct or 0) > 0
            and (self.dollar_volume_trend_pct or 0) > 10
            and self.above_sma50 is True
        )


class ThemeMarketRead(BaseModel):
    basket: BasketPerformance = Field(default_factory=BasketPerformance)
    etfs: list[EtfActivity] = Field(default_factory=list)
    etf_coverage: int = 0

    @property
    def etfs_accumulating(self) -> list[str]:
        return [e.etf for e in self.etfs if e.accumulating]


def load_series(db: Session, symbols: set[str], days: int = 400) -> Series:
    """One query for every symbol's daily bars. Shared by all callers."""
    if not symbols:
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(
        select(BarRow.symbol, BarRow.ts, BarRow.close, BarRow.volume)
        .where(BarRow.timeframe == "1Day", BarRow.ts >= cutoff, BarRow.symbol.in_(symbols))
        .order_by(BarRow.symbol, BarRow.ts)
    ).all()
    series: Series = defaultdict(list)
    for symbol, _ts, close, volume in rows:
        series[symbol].append((float(close), int(volume)))
    return dict(series)


def pct_return(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback or closes[-1 - lookback] <= 0:
        return None
    return (closes[-1] / closes[-1 - lookback] - 1) * 100


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def dollar_volume_trend(points: list[tuple[float, int]]) -> float | None:
    """Recent turnover versus its own recent baseline, as a percentage.

    Comparing the last 5 sessions to the 20 before them (not to the same
    window) is what makes this a *change* measure — an ETF that is always
    heavily traded scores 0, an ETF that just woke up scores high.
    """
    if len(points) < 26:
        return None
    recent = [c * v for c, v in points[-5:]]
    baseline = [c * v for c, v in points[-25:-5]]
    if not recent or not baseline:
        return None
    base = mean(baseline)
    if base <= 0:
        return None
    return (mean(recent) / base - 1) * 100


def basket_performance(
    series: Series, symbols: list[str], benchmark_closes: list[float]
) -> BasketPerformance:
    """Aggregate behaviour of a theme's constituents.

    Uses the MEDIAN return alongside the mean: a basket where one name
    tripled and eleven went nowhere has a flattering mean and an honest
    median, and the gap between them is itself the concentration warning.
    """
    bench_21 = pct_return(benchmark_closes, 21)
    bench_63 = pct_return(benchmark_closes, 63)

    returns_21: list[float] = []
    returns_63: list[float] = []
    above_50 = 0
    outperformers = 0
    counted = 0

    for symbol in symbols:
        points = series.get(symbol)
        if not points or len(points) < 25:
            continue
        closes = [c for c, _ in points]
        r21 = pct_return(closes, 21)
        if r21 is None:
            continue
        counted += 1
        returns_21.append(r21)
        r63 = pct_return(closes, 63)
        if r63 is not None:
            returns_63.append(r63)
        sma50 = sma(closes, 50)
        if sma50 is not None and closes[-1] > sma50:
            above_50 += 1
        if bench_21 is not None and r21 > bench_21:
            outperformers += 1

    if not counted:
        return BasketPerformance()

    avg_21 = mean(returns_21)
    avg_63 = mean(returns_63) if returns_63 else None
    ordered = sorted(returns_21)
    median_21 = ordered[len(ordered) // 2]

    # Share of total positive move attributable to the single best name.
    positive_total = sum(r for r in returns_21 if r > 0)
    best = max(returns_21)
    concentration = (
        round(best / positive_total * 100, 2) if positive_total > 0 and best > 0 else None
    )

    return BasketPerformance(
        symbols_with_data=counted,
        return_21d_pct=round(avg_21, 2),
        return_63d_pct=round(avg_63, 2) if avg_63 is not None else None,
        excess_21d_pct=round(avg_21 - bench_21, 2) if bench_21 is not None else None,
        excess_63d_pct=(
            round(avg_63 - bench_63, 2)
            if avg_63 is not None and bench_63 is not None
            else None
        ),
        breadth_pct=round(above_50 / counted * 100, 2),
        participation_pct=round(outperformers / counted * 100, 2),
        median_return_21d_pct=round(median_21, 2),
        concentration_pct=concentration,
    )


def etf_activity(series: Series, etfs: list[str], benchmark_closes: list[float]) -> list[EtfActivity]:
    """Flow proxy for each thematic ETF we have bars for."""
    bench_21 = pct_return(benchmark_closes, 21)
    out: list[EtfActivity] = []
    for etf in etfs:
        points = series.get(etf)
        if not points or len(points) < 25:
            continue
        closes = [c for c, _ in points]
        r21 = pct_return(closes, 21)
        sma50 = sma(closes, 50)
        sma200 = sma(closes, 200)
        adv = mean(c * v for c, v in points[-20:]) if len(points) >= 20 else None
        turnover = dollar_volume_trend(points)
        out.append(
            EtfActivity(
                etf=etf,
                return_21d_pct=round(r21, 2) if r21 is not None else None,
                excess_21d_pct=(
                    round(r21 - bench_21, 2)
                    if r21 is not None and bench_21 is not None
                    else None
                ),
                dollar_volume_trend_pct=round(turnover, 2) if turnover is not None else None,
                above_sma50=None if sma50 is None else closes[-1] > sma50,
                above_sma200=None if sma200 is None else closes[-1] > sma200,
                avg_dollar_volume20=round(adv, 0) if adv is not None else None,
            )
        )
    return out


def read_theme(
    series: Series, symbols: list[str], etfs: list[str], benchmark_closes: list[float]
) -> ThemeMarketRead:
    """Full market read for one theme."""
    activity = etf_activity(series, etfs, benchmark_closes)
    return ThemeMarketRead(
        basket=basket_performance(series, symbols, benchmark_closes),
        etfs=activity,
        etf_coverage=len(activity),
    )


def market_environment(db: Session) -> tuple[str, str]:
    """Bullish / Neutral / Bearish, from SPY's own trend and drawdown.

    Deliberately simple and deterministic: the report's environment line is
    context, not a call, and the real regime classifier (agents/regime.py,
    which also reads VIX) still drives everything that matters.
    """
    series = load_series(db, {BENCHMARK}, days=400)
    points = series.get(BENCHMARK)
    if not points or len(points) < 60:
        return "Neutral", "not enough benchmark history to classify the environment"

    closes = [c for c, _ in points]
    last = closes[-1]
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    window = closes[-252:]
    high = max(window) if window else last
    drawdown = (high - last) / high * 100 if high > 0 else 0.0
    r21 = pct_return(closes, 21) or 0.0

    above_50 = sma50 is not None and last > sma50
    above_200 = sma200 is not None and last > sma200

    if above_50 and above_200 and drawdown < 5:
        return "Bullish", (
            f"S&P proxy is above its 50- and 200-day averages, {drawdown:.1f}% "
            f"off its 52-week high ({r21:+.1f}% over the last month)"
        )
    if not above_50 and not above_200 and drawdown > 10:
        return "Bearish", (
            f"S&P proxy is below both its 50- and 200-day averages and {drawdown:.1f}% "
            f"off its 52-week high ({r21:+.1f}% over the last month)"
        )
    return "Neutral", (
        f"S&P proxy is mixed — {'above' if above_200 else 'below'} its 200-day average, "
        f"{drawdown:.1f}% off its 52-week high ({r21:+.1f}% over the last month)"
    )
