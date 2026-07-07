"""Scheduled jobs. Each wraps its own DB session and is market-hours aware.

Schedule (all times America/New_York, NYSE calendar):
  - every 15 min, market hours : intraday bars + quotes, then pipeline scan
  - every 5 min, market hours  : staleness watchdog
  - 08:30 ET trading days      : morning ingest (bars, news, fundamentals,
                                 earnings, macro, filings) + pre-open brief
  - 16:45 ET trading days      : post-close ingest + recap
  - 02:00 ET daily             : nightly evaluation (Phase 6)
"""

import structlog

from sentinel.data import ingest
from sentinel.data.market_hours import is_market_open, is_trading_day
from sentinel.data.watchdog import check_staleness
from sentinel.db.base import get_session_factory

log = structlog.get_logger()


def _session():
    return get_session_factory()()


def job_intraday_scan() -> None:
    if not is_market_open():
        return
    with _session() as db:
        try:
            ingest.ingest_bars(db, timeframe="15Min", lookback_days=5)
            ingest.ingest_quotes(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("intraday ingest failed")
            return
    _run_pipeline_scan()


def _run_pipeline_scan() -> None:
    """Run the signal pipeline (Phase 3+). No-op until then."""
    try:
        from sentinel.pipeline.runner import run_scan
    except ImportError:
        return
    with _session() as db:
        try:
            run_scan(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("pipeline scan failed")


def job_watchdog() -> None:
    with _session() as db:
        try:
            check_staleness(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("watchdog failed")


def job_morning() -> None:
    if not is_trading_day():
        return
    with _session() as db:
        try:
            ingest.ingest_bars(db, timeframe="1Day")
            ingest.ingest_news(db)
            ingest.ingest_fundamentals(db)
            ingest.ingest_earnings_calendar(db)
            ingest.ingest_macro(db)
            ingest.ingest_filings(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("morning ingest failed")
            return
    _send_brief("pre_open")


def job_post_close() -> None:
    if not is_trading_day():
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
    """Resolve signals and update strategy stats (Phase 6). No-op until then."""
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
