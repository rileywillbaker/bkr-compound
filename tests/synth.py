"""Deterministic synthetic market data for agent/pipeline tests.

No randomness: every generator is a pure function of its arguments so
assertions stay stable (golden-file style).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel.data.context import MarketContext, SymbolContext
from sentinel.providers.types import Bar, MacroPoint, NewsItem

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def make_bars(
    symbol: str,
    n: int,
    start: float = 100.0,
    drift: float = 0.3,
    alternate: float = 0.0,
    growth: float | None = None,
    volume: int = 1_000_000,
    last_volume: int | None = None,
    start_day: int = 0,
) -> list[Bar]:
    """Linear-drift daily bars; `alternate` adds a +/- zigzag (rangebound);
    `growth` switches to a geometric series (constant % return). `start_day`
    offsets timestamps so segments can be concatenated into one history."""
    bars: list[Bar] = []
    for i in range(n):
        zig = alternate if i % 2 else -alternate
        base = start * growth**i if growth is not None else start + drift * i
        price = base + zig
        vol = last_volume if (last_volume is not None and i == n - 1) else volume
        bars.append(
            Bar(
                symbol=symbol,
                ts=T0 + timedelta(days=start_day + i),
                open=Decimal(str(round(price - 0.2, 4))),
                high=Decimal(str(round(price + 0.6, 4))),
                low=Decimal(str(round(price - 0.6, 4))),
                close=Decimal(str(round(price, 4))),
                volume=vol,
                timeframe="1Day",
            )
        )
    return bars


def make_vix(value: float, n: int = 5) -> list[MacroPoint]:
    return [
        MacroPoint(series_id="VIXCLS", date=(T0 + timedelta(days=i)).date(), value=value)
        for i in range(n)
    ]


def make_news(symbol: str, headlines: list[str]) -> list[NewsItem]:
    return [
        NewsItem(
            provider_id=f"{symbol}-{i}",
            symbol=symbol,
            headline=h,
            source="synthetic",
            published_at=T0 + timedelta(days=400, hours=i),
        )
        for i, h in enumerate(headlines)
    ]


def make_context(
    symbols: dict[str, SymbolContext],
    spy_bars: list[Bar] | None = None,
    macro: dict[str, list[MacroPoint]] | None = None,
) -> MarketContext:
    return MarketContext(
        as_of=T0 + timedelta(days=400),
        spy_bars=spy_bars if spy_bars is not None else make_bars("SPY", 250, start=400.0),
        macro=macro or {},
        symbols=symbols,
    )


def make_symbol_context(
    symbol: str,
    bars: list[Bar] | None = None,
    news: list[NewsItem] | None = None,
    sector: str = "Technology",
    market_cap: float | None = 50_000.0,
    pe: float | None = 25.0,
    ps: float | None = 6.0,
) -> SymbolContext:
    return SymbolContext(
        symbol=symbol,
        daily_bars=bars if bars is not None else make_bars(symbol, 250),
        news=news or [],
        sector=sector,
        market_cap=market_cap,
        pe=pe,
        ps=ps,
    )
