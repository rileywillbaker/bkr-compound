"""Scheduled jobs. Each wraps its own DB session and is market-hours aware.

Schedule (all times America/New_York, NYSE calendar). Scans run exactly
THREE times per trading day — there is no rolling intraday scan:
  - 08:30 ET : tiered pre-market ingest + discovery + pre-open brief. Bars and
               macro cover the FULL universe (cheap, and every technical
               trigger needs them); the expensive per-symbol calls — news,
               filings, insider transactions, fundamentals, quotes — are
               pointed at a deterministically-ranked focus set instead, which
               is what lets the universe grow past 700 names without the
               rate-limit budget growing with it. Starts an hour before the
               open because the rate-limited sweep takes a while. No signal
               alerts fire from this pass.
  - 09:30 ET : market-open confirmation scan (candidates + watchlist +
               positions). Only BUY alerts may fire.
  - 15:30 ET : near-close exit scan. Only SELL alerts may fire.
  - every 5 min, market hours : staleness watchdog (no LLM, no alerts)
  - 16:45 ET trading days     : post-close ingest + recap
  - 02:00 ET mon-fri          : nightly evaluation (Phase 6)

The SEPARATE swing book (sentinel/swing/) adds two of its own scans that do
NOT touch the three core scans above: 09:45 ET (open) and 12:30 ET (midday),
each running the swing pipeline with its own alert channel + daily cap.

Weekends are fully dark: every job is cron'd mon-fri AND double-checked by
_weekend_or_closed(), so Saturdays/Sundays see no ingestion, no LLM calls,
and no alerts even if a cron entry is misconfigured.
"""

import threading
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


# The boot-time run (scheduler/run.py) and the scheduled 08:30 run can
# overlap when the rate-limited universe sweep runs long (e.g. the host
# slept mid-run); two concurrent sweeps halve each other's rate budget and
# can take days. Second entrant just skips.
_premarket_running = threading.Lock()


def job_premarket_discovery() -> None:
    """08:30 ET: tiered ingest, then build the day's candidate list.

    Order matters: discovery reads only the DB, so everything it needs must
    land first. No pipeline run, no signal alerts here.
    """
    if _weekend_or_closed():
        return
    if not _premarket_running.acquire(blocking=False):
        log.warning("pre-market ingest+discovery already running; skipping this run")
        return
    try:
        _premarket_body()
    finally:
        _premarket_running.release()


def _premarket_body() -> None:
    # --- Tier 1: universe-wide, cheap. Bars power every technical trigger,
    # every screen, and the whole discovery engine, so they always cover
    # everything. Macro and the earnings calendar are single calls.
    with _session() as db:
        try:
            ingest.ingest_bars(db, timeframe="1Day")
            ingest.ingest_macro(db)
            ingest.ingest_earnings_calendar(db)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("pre-market bar/macro ingest failed")
            return

    # --- Tier 2: per-symbol and rate-limited. Scoped to the focus set —
    # the deterministic technical shortlist plus yesterday's candidates, the
    # watchlist and everything held. Set full_universe_deep_ingest to restore
    # the old sweep-everything behaviour.
    with _session() as db:
        try:
            from sentinel.data.discovery import get_deep_data_symbols
            from sentinel.db.settings_store import focus_set_size, full_universe_deep_ingest

            deep = (
                None
                if full_universe_deep_ingest(db)
                else get_deep_data_symbols(db, focus_limit=focus_set_size(db))
            )
            log.info(
                "pre-market deep ingest scope",
                symbols="full universe" if deep is None else len(deep),
            )
            ingest.ingest_news(db, symbols=deep)
            ingest.ingest_insider_transactions(db, symbols=deep)
            ingest.ingest_filings(db, symbols=deep)
            ingest.ingest_fundamentals(db, symbols=deep)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("pre-market deep ingest failed")
            # discovery can still run on bars alone — keep going

    # --- Tier 3: discovery, then top up data for whatever it surfaced.
    with _session() as db:
        try:
            from sentinel.data.discovery import discover, get_scan_symbols

            discover(db)
            scan_set = get_scan_symbols(db)
            # Bars again here (in addition to the full-universe pass above):
            # a discovery trigger (e.g. finviz_screen) can surface a symbol
            # outside the static universe that never got bars in that pass.
            ingest.ingest_bars(db, timeframe="1Day", symbols=scan_set)
            ingest.ingest_fundamentals(db, symbols=scan_set)
            ingest.ingest_quotes(db, symbols=scan_set)
            ingest.ingest_short_interest(db, symbols=scan_set)
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


def _run_swing_scan() -> None:
    """Run the SEPARATE swing-book pipeline (sentinel/swing/). Independent of
    the three core scans above; swing alerts have their own daily cap."""
    try:
        from sentinel.swing.pipeline import run_swing_scan
    except ImportError:
        return
    with _session() as db:
        try:
            run_swing_scan(db, send_alerts=True)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("swing scan failed")


def job_swing_open() -> None:
    """09:45 ET: swing setup scan shortly after the open. Swing alerts may fire."""
    if _weekend_or_closed() or not is_market_open():
        return
    _run_swing_scan()


def job_swing_midday() -> None:
    """12:30 ET: midday swing setup refresh. Swing alerts may fire."""
    if _weekend_or_closed() or not is_market_open():
        return
    _run_swing_scan()


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
    """Resolve signals, update strategy stats (Phase 6), and sweep expired
    cache rows. Deterministic; still skipped on weekends per the
    no-weekend-runs rule (Friday's signals resolve on the next trading
    night)."""
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
    with _session() as db:
        try:
            from sentinel.data.cache import purge_expired

            purged = purge_expired(db)
            db.commit()
            log.info("cache housekeeping", purged=purged)
        except Exception:
            db.rollback()
            log.exception("cache purge failed")


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
