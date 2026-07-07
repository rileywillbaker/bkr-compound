"""Ingestion jobs: pull from providers, upsert into TimescaleDB, log to
system_events. Each job is independent and safe to re-run (idempotent upserts).
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from sentinel.config import get_settings
from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FilingRow,
    FundamentalsRow,
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

MARKET_SYMBOLS = ["SPY"]  # always ingested for regime detection


def _log(db: Session, kind: str, message: str, level: str = "INFO", payload: dict | None = None):
    db.add(SystemEvent(kind=kind, message=message, level=level, payload=payload))
    db.flush()


def _universe(db: Session) -> list[str]:
    """Watchlist + regime symbols + any held positions (positions from Phase 2)."""
    symbols = set(get_settings().watchlist_symbols) | set(MARKET_SYMBOLS)
    try:
        from sentinel.db import models as _m

        position = getattr(_m, "Position", None)  # added in Phase 2
        if position is not None:
            for (sym,) in db.query(position.symbol).filter(position.shares != 0).all():
                symbols.add(sym)
    except Exception:
        pass
    return sorted(symbols)


def ingest_bars(db: Session, timeframe: str = "1Day", lookback_days: int = 400) -> int:
    """Upsert bars for the whole universe. Returns row count."""
    try:
        md = build_market_data(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.bars", str(exc), level="WARN")
        return 0
    start = datetime.now(UTC) - timedelta(days=lookback_days)
    total = 0
    for symbol in _universe(db):
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


def ingest_quotes(db: Session) -> int:
    try:
        md = build_market_data(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.quotes", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in _universe(db):
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


def ingest_news(db: Session, days: int = 3) -> int:
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
    for symbol in _universe(db):
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


def ingest_fundamentals(db: Session) -> int:
    try:
        research = build_research(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.fundamentals", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in _universe(db):
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


def ingest_filings(db: Session, forms: list[str] | None = None) -> int:
    forms = forms or ["8-K", "10-Q", "10-K", "4"]
    try:
        filings = build_filings(db)
    except CredentialsMissing as exc:
        _log(db, "ingest.filings", str(exc), level="WARN")
        return 0
    count = 0
    for symbol in _universe(db):
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
