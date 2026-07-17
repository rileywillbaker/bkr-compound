"""Worker entrypoint: `python -m sentinel.scheduler.run`.

Scan cadence (spec update 2026-07): exactly three scans per trading day —
08:30 pre-market discovery (started early so the ~25-minute rate-limited
universe ingest is done well before the open), 09:30 open confirmation
(BUY alerts), 15:30 near-close exit scan (SELL alerts). The old
15/30-minute rolling intraday scan is gone. Every job is mon-fri only;
jobs.py re-checks the weekend/holiday guard internally as well.

Host-sleep resilience: on a desktop deployment (Docker Desktop / WSL2) the
guest VM's monotonic clock PAUSES while the host sleeps, so APScheduler's
internal Event.wait() until the next fire time can stall days past it and
never run anything (observed 2026-07: zero jobs fired for a week). Two
countermeasures here: the main loop forces `sched.wakeup()` every 30s so
due jobs are re-evaluated against the NTP-resynced wall clock, and every
job carries a misfire_grace_time wide enough to run late-but-useful after
the host wakes mid-morning.
"""

import threading
import time
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.background import BackgroundScheduler

from sentinel.scheduler import jobs

ET = ZoneInfo("America/New_York")
log = structlog.get_logger()


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)
    # Grace times are deliberately generous (hours, not minutes): if the host
    # slept through a fire time, running late once it wakes still beats not
    # running at all. Jobs with a hard market-hours dependency re-check it
    # internally (job_close_scan), so a wide grace here is safe.
    sched.add_job(
        jobs.job_premarket_discovery,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=30,
        id="premarket_discovery",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=4 * 3600,
    )
    sched.add_job(
        jobs.job_market_open_scan,
        "cron",
        day_of_week="mon-fri",
        hour=9,
        minute=30,
        id="market_open_scan",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3 * 3600,
    )
    sched.add_job(
        jobs.job_close_scan,
        "cron",
        day_of_week="mon-fri",
        hour=15,
        minute=30,
        id="close_scan",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,  # job no-ops itself once the market closes
    )
    sched.add_job(
        jobs.job_watchdog,
        "cron",
        day_of_week="mon-fri",
        hour="9-16",
        minute="*/5",
        id="watchdog",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    sched.add_job(
        jobs.job_post_close,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=45,
        id="post_close",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=6 * 3600,
    )
    sched.add_job(
        jobs.job_nightly_evaluation,
        "cron",
        day_of_week="mon-fri",
        hour=2,
        minute=0,
        id="nightly_eval",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=12 * 3600,
    )
    return sched


def _boot_ingest() -> None:
    """Pre-market ingest+discovery on boot so a fresh install has data.
    Idempotent upserts + the weekend/holiday guard make it safe to repeat.
    Runs in its own thread: the rate-limited universe sweep takes ~25 minutes
    (or far longer if the host sleeps mid-run) and must never block the
    wakeup loop in main(). jobs.py's non-reentrancy guard prevents overlap
    with the scheduled 08:30 run."""
    try:
        jobs.job_premarket_discovery()
    except Exception:
        log.exception("boot ingest failed")


def main() -> None:
    log.info("worker starting")
    sched = build_scheduler()
    sched.start()
    threading.Thread(target=_boot_ingest, name="boot-ingest", daemon=True).start()
    try:
        while True:
            time.sleep(30)
            # Force due-job re-evaluation against the wall clock; APScheduler's
            # own long monotonic wait stalls across host sleeps (see module
            # docstring). Cheap no-op when nothing is due.
            sched.wakeup()
    except KeyboardInterrupt:
        sched.shutdown()


if __name__ == "__main__":
    main()
