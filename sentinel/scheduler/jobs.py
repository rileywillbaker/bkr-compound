"""Scheduled jobs. Each wraps its own DB session and is market-hours aware.

Schedule (all times America/New_York, NYSE calendar). Scans run exactly
THREE times per trading day — there is no rolling intraday scan:
  - 08:30 ET : pre-market ingest over the FULL universe (bars, news,
               earnings, macro, insider txns, filings) + discovery run that
               builds the day's candidate list + pre-open brief. Starts an
               hour before the open because the rate-limited universe sweep
               takes ~25 minutes. No signal alerts fire from this pass.
  - 09:30 ET : market-open confirmation scan (candidates + watchlist +
               positions). Only BUY alerts may fire.
  - 15:30 ET : near-close exit scan. Only SELL alerts may fire.
  - every 5 min, market hours : staleness watchdog (no LLM, no alerts)
  - 16:45 ET trading days     : post-close ingest + recap
  - 02:00 ET mon-fri          : nightly evaluation (Phase 6)

Weekends are fully dark: every job is cron'd mon-fri AND double-checked by
_weekend_or_closed(), so Saturdays/Sundays see no ingestion, no LLM calls,
and no alerts even if a cron entry is misconfigured.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from sentinel.data import ingest
from sentinel.data.market_hours import is_market_open, is_trading_day
from sentinel.data.watchdog import check_staleness
from sentinel.db.base import get_session_factory

ET = ZoneInfo("America/New_York")
log = structlog.get_logger()


def _session():
    return get_session_factory()()


def _weekend_or_closed() -> bool:
    """Hard stop for Saturday/Sunday (ET) plus exchange holidays."""
    if datetime.now(ET).weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return True
    return not is_trading_day()


def _scan_symbols() -> list[str]:
    """Candidates + watchlist + held positions (see data/discovery.py)."""
    from sentinel.data.discovery import get_scan_symbols

    with _session() as db:
        return get_scan_symbols(db)


def job_premarket_discovery() -> None:
    """08:30 ET: full-universe ingest, then build the day's candidate list.

    Order matters: discovery reads only the DB, so everything it needs must
    land first. Fundamentals and quotes are then refreshed for just the scan
    set (candidates + watchlist + positions) to stay inside free-tier rate
    limits. No pipeline run, no signal alerts here.
    """
    if _weekend_or_closed():
        return
    with _session() as db:
        try:
            ingest.ingest_bars(db, timeframe="1Day")
            ingest.ingest_news(db)
            ingest.ingest_earnings_calendar(db)
            ingest.ingest_macro(db)
            ingest.ingest_insider_transactions(db)
            ingest.ingest_filings(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("pre-market ingest failed")
            return
    with _session() as db:
        try:
            from sentinel.data.discovery import discover, get_scan_symbols

            discover(db)
            scan_set = get_scan_symbols(db)
            ingest.ingest_fundamentals(db, symbols=scan_set)
            ingest.ingest_quotes(db, symbols=scan_set)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("discovery failed")
            return
    _send_brief("pre_open")


def job_market_open_scan() -> None:
    """09:30 ET: confirmation scan. Only BUY alerts may fire."""
    if _weekend_or_closed():
        return
    _run_pipeline_scan(alert_actions=frozenset({"BUY"}))


def job_close_scan() -> None:
    """15:30 ET: near-close sell/exit scan. Only SELL alerts may fire."""
    if _weekend_or_closed():
        return
    if not is_market_open():
        return
    with _session() as db:
        try:
            # refresh today's (partial) daily bars + quotes for the scan set
            symbols = sorted(set(_scan_symbols()) | {"SPY"})
            ingest.ingest_bars(db, timeframe="1Day", lookback_days=7, symbols=symbols)
            ingest.ingest_quotes(db, symbols=symbols)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("close-scan ingest failed")
    _run_pipeline_scan(alert_actions=frozenset({"SELL"}))


def _run_pipeline_scan(alert_actions: frozenset[str]) -> None:
    """Run the signal pipeline on the day's scan set (Phase 3+)."""
    try:
        from sentinel.pipeline.runner import run_scan
    except ImportError:
        return
    with _session() as db:
        try:
            run_scan(db, alert_actions=alert_actions)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("pipeline scan failed")


def job_watchdog() -> None:
    if _weekend_or_closed():
        return
    with _session() as db:
        try:
            check_staleness(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("watchdog failed")


def job_post_close() -> None:
    if _weekend_or_closed():
        return
    with _session() as db:
        try:
            ingest.ingest_bars(db, timeframe="1Day", lookback_days=7)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("post-close ingest failed")
            return
    _send_brief("post_close")


def job_nightly_evaluation() -> None:
    """Resolve signals and update strategy stats (Phase 6). Deterministic;
    still skipped on weekends per the no-weekend-runs rule (Friday's signals
    resolve on the next trading night)."""
    if datetime.now(ET).weekday() >= 5:
        return
    try:
        from sentinel.evaluation.resolve import run_nightly
    except ImportError:
        return
    with _session() as db:
        try:
            run_nightly(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("nightly evaluation failed")


def _send_brief(kind: str) -> None:
    """Daily pre-open brief / post-close recap (Phase 4). No-op until then."""
    try:
        from sentinel.alerts.briefs import send_brief
    except ImportError:
        return
    with _session() as db:
        try:
            send_brief(db, kind)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("brief failed", kind=kind)
