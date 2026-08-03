"""Deterministic fixtures for the Trend Discovery Agent tests.

Same philosophy as tests/synth.py: no randomness anywhere, so every assertion
is stable. These helpers seed the DATABASE (rather than building in-memory
context objects) because the trend agent reads everything from stored rows.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from sentinel.db.models import (
    BarRow,
    EtfHoldingRow,
    FundamentalsRow,
    SocialMentionRow,
    TrendDocumentRow,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def seed_bars(
    db: Session,
    symbol: str,
    n: int = 260,
    start: float = 100.0,
    drift: float = 0.2,
    volume: int = 2_000_000,
    last_volume: int | None = None,
    end: datetime | None = None,
) -> None:
    """Linear-drift daily bars ending today (so recency windows match).

    Timestamps are snapped to midnight UTC so every symbol seeded in a test
    shares identical bar timestamps. Real daily bars align this way, and
    without it each call would stamp its own microsecond offset — correlations
    would then find no overlapping rows and the risk engine's
    max_correlated_exposure rule would fail closed for fixture reasons rather
    than real ones.
    """
    finish = (end or datetime.now(UTC)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    for i in range(n):
        price = start + drift * i
        ts = finish - timedelta(days=(n - 1 - i))
        vol = last_volume if (last_volume is not None and i == n - 1) else volume
        db.add(
            BarRow(
                symbol=symbol,
                timeframe="1Day",
                ts=ts,
                open=Decimal(str(round(price - 0.2, 4))),
                high=Decimal(str(round(price + 0.6, 4))),
                low=Decimal(str(round(price - 0.6, 4))),
                close=Decimal(str(round(price, 4))),
                volume=vol,
            )
        )
    db.flush()


def seed_fundamentals(
    db: Session,
    symbol: str,
    name: str = "",
    sector: str = "Energy",
    market_cap: float = 12_000.0,
    pe: float | None = 20.0,
    ps: float | None = 4.0,
    revenue_growth: float | None = 18.0,
    eps_growth: float | None = 12.0,
    beta: float | None = 1.1,
) -> None:
    db.add(
        FundamentalsRow(
            symbol=symbol,
            name=name or f"{symbol} Corporation",
            sector=sector,
            market_cap=market_cap,
            pe=pe,
            ps=ps,
            revenue_growth_ttm=revenue_growth,
            eps_growth_ttm=eps_growth,
            beta=beta,
        )
    )
    db.flush()


def seed_document(
    db: Session,
    key: str,
    title: str,
    channel: str = "news",
    source: str = "google_news:nuclear",
    themes: list[str] | None = None,
    symbols: list[str] | None = None,
    sentiment: float = 0.4,
    days_ago: float = 1.0,
    engagement: int = 0,
) -> None:
    db.add(
        TrendDocumentRow(
            doc_key=key,
            source=source,
            channel=channel,
            title=title,
            summary="",
            url=f"https://example.test/{key}",
            published_at=datetime.now(UTC) - timedelta(days=days_ago),
            themes=themes or ["nuclear"],
            symbols=symbols or [],
            sentiment=sentiment,
            engagement=engagement,
        )
    )
    db.flush()


def seed_theme_corpus(
    db: Session,
    theme: str = "nuclear",
    recent_news: int = 18,
    baseline_news: int = 6,
    gov: int = 4,
    social: int = 3,
    symbols: list[str] | None = None,
) -> None:
    """A theme with accelerating coverage: many recent articles against a
    thin baseline, plus government activity and some discussion."""
    tickers = symbols or ["CCJ", "UEC"]
    for i in range(recent_news):
        seed_document(
            db,
            key=f"{theme}-recent-{i}",
            title=f"Nuclear power capacity expands as reactor restart advances ({i})",
            themes=[theme],
            symbols=tickers if i % 2 == 0 else [],
            days_ago=1.0,
        )
    for i in range(baseline_news):
        seed_document(
            db,
            key=f"{theme}-base-{i}",
            title=f"Nuclear energy briefing ({i})",
            themes=[theme],
            days_ago=12.0,
        )
    for i in range(gov):
        seed_document(
            db,
            key=f"{theme}-gov-{i}",
            title=f"NRC issues advanced reactor licensing rule ({i})",
            channel="gov",
            source="federal_register",
            themes=[theme],
            days_ago=2.0,
        )
    for i in range(social):
        seed_document(
            db,
            key=f"{theme}-social-{i}",
            title=f"$CCJ uranium supply looks tight ({i})",
            channel="social",
            source="reddit:stocks",
            themes=[theme],
            symbols=["CCJ"],
            days_ago=1.0,
            engagement=40,
        )


def seed_social(
    db: Session, symbol: str, mentions: int, day_offset: int = 0, sentiment: float = 0.5
) -> None:
    db.add(
        SocialMentionRow(
            symbol=symbol,
            source="reddit",
            day=date.today() - timedelta(days=day_offset),
            mentions=mentions,
            sentiment=sentiment,
            positive=mentions,
            negative=0,
            engagement=mentions * 10,
        )
    )
    db.flush()


def seed_holdings(
    db: Session,
    etf: str,
    weights: dict[str, float],
    days_ago: int = 0,
) -> None:
    for symbol, weight in weights.items():
        db.add(
            EtfHoldingRow(
                etf=etf,
                symbol=symbol,
                as_of=date.today() - timedelta(days=days_ago),
                weight_pct=weight,
                shares=weight * 10_000,
                market_value=weight * 1_000_000,
                name=f"{symbol} Corporation",
            )
        )
    db.flush()


def seed_equity(db: Session, equity: float = 5_000.0) -> None:
    from sentinel.db.settings_store import set_setting

    set_setting(db, "starting_equity", equity)
