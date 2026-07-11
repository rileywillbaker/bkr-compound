"""Worker entrypoint: `python -m sentinel.scheduler.run`.

Scan cadence (spec update 2026-07): exactly three scans per trading day —
08:30 pre-market discovery (started early so the ~25-minute rate-limited
universe ingest is done well before the open), 09:30 open confirmation
(BUY alerts), 15:30 near-close exit scan (SELL alerts). The old
15/30-minute rolling intraday scan is gone. Every job is mon-fri only;
jobs.py re-checks the weekend/holiday guard internally as well.
"""

import time
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.background import BackgroundScheduler

from sentinel.scheduler import jobs

ET = ZoneInfo("America/New_York")
log = structlog.get_logger()


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)
    sched.add_job(
        jobs.job_premarket_discovery,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=30,
        id="premarket_discovery",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
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
        misfire_grace_time=600,
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
        misfire_grace_time=600,
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
    )
    sched.add_job(
        jobs.job_post_close,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=45,
        id="post_close",
    )
    sched.add_job(
        jobs.job_nightly_evaluation,
        "cron",
        day_of_week="mon-fri",
        hour=2,
        minute=0,
        id="nightly_eval",
    )
    return sched


def main() -> None:
    log.info("worker starting")
    sched = build_scheduler()
    sched.start()
    # Run the pre-market ingest+discovery on boot so a fresh install has data.
    # Idempotent upserts + the weekend/holiday guard make this safe to repeat.
    try:
        jobs.job_premarket_discovery()
    except Exception:
        log.exception("boot ingest failed")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        sched.shutdown()


if __name__ == "__main__":
    main()
