"""Ingestion jobs: pull from providers, upsert into TimescaleDB, log to
system_events. Each job is independent and safe to re-run (idempotent upserts).

Jobs default to the FULL static universe (S&P 500 + watchlist + positions,
see sentinel.data.universe) so discovery can monitor every name; pass
`symbols` to restrict a run (e.g. fundamentals/quotes for just the day's
scan set, or an on-demand backfill for one ticker). All provider calls go
through the shared rate limiter, so universe-wide runs are slow-but-safe on
free tiers rather than bursty.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from sentinel.data.universe import MARKET_SYMBOLS, get_universe
from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FilingRow,
    FundamentalsRow,
    InsiderTransactionRow,
    MacroSeriesRow,
    NewsItemRow,
    QuoteLatest,
    SystemEvent,
)
from sentinel.providers.base import ProviderError, ProviderUnavailable
from sentinel.providers.macro.fred import CORE_SERIES
from sentinel.providers.registry import (
    CredentialsMissing,
    build_filings,
    build_macro,
    build_market_data,
    build_research,
)


def _log(db: Session, kind: str, message: str, level: str = "INFO", payload: dict | None = None):
    db.add(SystemEvent(kind=kind, message=message, level=level, payload=payload))
    db.flush()


def _universe(db: Session) -> list[str]:
    """Full universe: static S&P 500 list + watchlist + held positions + SPY."""
    return get_universe(db)


def ingest_bars(
    db: Session,
    timeframe: str = "1Day",
    lookback_days: int = 400,
    symbols: list[str] | None = None,
) -> int:
    """Upsert bars (whole universe by default). Returns row count."""
    try:
        md = build_market_data(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.bars", str(exc), level="WARN")
        return 0
    start = datetime.now(UTC) - timedelta(days=lookback_days)
    total = 0
    for symbol in symbols or _universe(db):
        try:
            bars = md.get_bars(symbol, timeframe, start)
        except ProviderError as exc:
            _log(db, "ingest.bars", f"{symbol}: {exc}", level="ERROR")
            continue
        for bar in bars:
            stmt = (
                pg_insert(BarRow)
                .values(
                    symbol=bar.symbol,
                    timeframe=bar.timeframe,
                    ts=bar.ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
                .on_conflict_do_update(
                    index_elements=["symbol", "timeframe", "ts"],
                    set_={
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    },
                )
            )
            db.execute(stmt)
            total += 1
    _log(db, "ingest.bars", f"{total} bars ({timeframe})")
    return total


def ingest_quotes(db: Session, symbols: list[str] | None = None) -> int:
    try:
        md = build_market_data(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.quotes", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in symbols or _universe(db):
        try:
            q = md.get_latest_quote(symbol)
        except ProviderError as exc:
            _log(db, "ingest.quotes", f"{symbol}: {exc}", level="ERROR")
            continue
        stmt = (
            pg_insert(QuoteLatest)
            .values(symbol=q.symbol, ts=q.ts, bid=q.bid, ask=q.ask, last=q.last)
            .on_conflict_do_update(
                index_elements=["symbol"],
                set_={"ts": q.ts, "bid": q.bid, "ask": q.ask, "last": q.last},
            )
        )
        db.execute(stmt)
        count += 1
    _log(db, "ingest.quotes", f"{count} quotes")
    return count


def ingest_news(db: Session, days: int = 3, symbols: list[str] | None = None) -> int:
    try:
        research = build_research(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.news", str(exc), level="WARN")
        return 0
    end = date.today()
    start = end - timedelta(days=days)
    count = 0
    items = []
    try:
        items.extend(research.market_news())
    except ProviderError as exc:
        _log(db, "ingest.news", f"market: {exc}", level="ERROR")
    for symbol in symbols or _universe(db):
        try:
            items.extend(research.company_news(symbol, start, end))
        except ProviderError as exc:
            _log(db, "ingest.news", f"{symbol}: {exc}", level="ERROR")
    for item in items:
        stmt = (
            pg_insert(NewsItemRow)
            .values(
                provider_id=item.provider_id,
                symbol=item.symbol,
                headline=item.headline,
                summary=item.summary,
                source=item.source,
                url=item.url,
                published_at=item.published_at,
            )
            .on_conflict_do_nothing(constraint="uq_news_provider_symbol")
        )
        db.execute(stmt)
        count += 1
    _log(db, "ingest.news", f"{count} items")
    return count


def ingest_fundamentals(db: Session, symbols: list[str] | None = None) -> int:
    try:
        research = build_research(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.fundamentals", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in symbols or _universe(db):
        values: dict = {"symbol": symbol, "as_of": datetime.now(UTC)}
        try:
            profile = research.company_profile(symbol)
            values.update(
                name=profile.name,
                sector=profile.sector,
                market_cap=profile.market_cap,
                exchange=profile.exchange,
            )
        except ProviderUnavailable:
            pass
        except ProviderError as exc:
            _log(db, "ingest.fundamentals", f"{symbol} profile: {exc}", level="ERROR")
            continue
        try:
            fin = research.basic_financials(symbol)
            values.update(
                pe=fin.pe,
                ps=fin.ps,
                eps_growth_ttm=fin.eps_growth_ttm,
                revenue_growth_ttm=fin.revenue_growth_ttm,
                beta=fin.beta,
                week52_high=fin.week52_high,
                week52_low=fin.week52_low,
            )
        except ProviderUnavailable:
            pass
        except ProviderError as exc:
            _log(db, "ingest.fundamentals", f"{symbol} metrics: {exc}", level="ERROR")
        stmt = (
            pg_insert(FundamentalsRow)
            .values(**values)
            .on_conflict_do_update(index_elements=["symbol"], set_=values)
        )
        db.execute(stmt)
        count += 1
    _log(db, "ingest.fundamentals", f"{count} symbols")
    return count


def ingest_earnings_calendar(db: Session, horizon_days: int = 21) -> int:
    try:
        research = build_research(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.earnings", str(exc), level="WARN")
        return 0
    start = date.today()
    end = start + timedelta(days=horizon_days)
    try:
        events = research.earnings_calendar(start, end)
    except ProviderUnavailable as exc:
        _log(db, "ingest.earnings", str(exc), level="WARN")
        return 0
    except ProviderError as exc:
        _log(db, "ingest.earnings", str(exc), level="ERROR")
        return 0
    count = 0
    for e in events:
        values = {
            "symbol": e.symbol,
            "date": e.date,
            "hour": e.hour,
            "eps_estimate": e.eps_estimate,
            "eps_actual": e.eps_actual,
            "revenue_estimate": e.revenue_estimate,
            "revenue_actual": e.revenue_actual,
        }
        stmt = (
            pg_insert(EarningsCalendarRow)
            .values(**values)
            .on_conflict_do_update(index_elements=["symbol", "date"], set_=values)
        )
        db.execute(stmt)
        count += 1
    _log(db, "ingest.earnings", f"{count} events")
    return count


def ingest_macro(db: Session, lookback_days: int = 730) -> int:
    try:
        macro = build_macro(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.macro", str(exc), level="WARN")
        return 0
    start = date.today() - timedelta(days=lookback_days)
    count = 0
    for series_id in CORE_SERIES:
        try:
            points = macro.get_series(series_id, start)
        except ProviderError as exc:
            _log(db, "ingest.macro", f"{series_id}: {exc}", level="ERROR")
            continue
        for p in points:
            stmt = (
                pg_insert(MacroSeriesRow)
                .values(series_id=p.series_id, date=p.date, value=p.value)
                .on_conflict_do_update(
                    index_elements=["series_id", "date"], set_={"value": p.value}
                )
            )
            db.execute(stmt)
            count += 1
    _log(db, "ingest.macro", f"{count} points")
    return count


def ingest_filings(
    db: Session, forms: list[str] | None = None, symbols: list[str] | None = None
) -> int:
    forms = forms or ["8-K", "10-Q", "10-K", "4"]
    try:
        filings = build_filings(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.filings", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in symbols or _universe(db):
        if symbol in MARKET_SYMBOLS or symbol in ("SPY", "QQQ"):
            continue  # ETFs don't file
        try:
            items = filings.recent_filings(symbol, forms)
        except ProviderError as exc:
            _log(db, "ingest.filings", f"{symbol}: {exc}", level="ERROR")
            continue
        for f in items:
            values = {
                "accession_no": f.accession_no,
                "symbol": f.symbol,
                "cik": f.cik,
                "form": f.form,
                "filed_at": f.filed_at,
                "url": f.url,
                "description": f.description,
            }
            stmt = (
                pg_insert(FilingRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["accession_no"])
            )
            db.execute(stmt)
            count += 1
    _log(db, "ingest.filings", f"{count} filings")
    return count


def ingest_insider_transactions(db: Session, symbols: list[str] | None = None) -> int:
    """Insider filings for discovery's buying-cluster trigger."""
    try:
        research = build_research(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.insiders", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in symbols or _universe(db):
        if symbol in MARKET_SYMBOLS or symbol in ("SPY", "QQQ"):
            continue  # ETFs have no insiders
        try:
            txns = research.insider_transactions(symbol)
        except ProviderUnavailable:
            continue
        except ProviderError as exc:
            _log(db, "ingest.insiders", f"{symbol}: {exc}", level="ERROR")
            continue
        for t in txns:
            stmt = (
                pg_insert(InsiderTransactionRow)
                .values(
                    symbol=t.symbol,
                    name=t.name,
                    share_change=t.share_change,
                    transaction_date=t.transaction_date,
                    transaction_price=t.transaction_price,
                    filing_date=t.filing_date,
                )
                .on_conflict_do_nothing(constraint="uq_insider_txn")
            )
            db.execute(stmt)
            count += 1
    _log(db, "ingest.insiders", f"{count} transactions")
    return count


def ensure_symbol_data(db: Session, symbol: str) -> None:
    """On-demand backfill so ANY ticker — even outside the static universe —
    can go through the full pipeline (chat: "Should I buy XYZ?"). Only
    fetches what is missing; provider failures degrade to whatever data
    exists (the pipeline handles gaps)."""
    symbol = symbol.strip().upper()
    has_bars = db.execute(
        select(BarRow.symbol)
        .where(BarRow.symbol == symbol, BarRow.timeframe == "1Day")
        .limit(1)
    ).first()
    if not has_bars:
        ingest_bars(db, timeframe="1Day", symbols=[symbol])
    if db.get(FundamentalsRow, symbol) is None:
        ingest_fundamentals(db, symbols=[symbol])
    has_news = db.execute(
        select(NewsItemRow.id).where(NewsItemRow.symbol == symbol).limit(1)
    ).first()
    if not has_news:
        ingest_news(db, symbols=[symbol])
