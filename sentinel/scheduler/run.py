"""Worker entrypoint: `python -m sentinel.scheduler.run`."""

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
        jobs.job_intraday_scan,
        "cron",
        day_of_week="mon-fri",
        hour="9-16",
        minute="*/15",
        id="intraday_scan",
        coalesce=True,
        max_instances=1,
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
        jobs.job_morning, "cron", day_of_week="mon-fri", hour=8, minute=30, id="morning"
    )
    sched.add_job(
        jobs.job_post_close,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=45,
        id="post_close",
    )
    sched.add_job(jobs.job_nightly_evaluation, "cron", hour=2, minute=0, id="nightly_eval")
    return sched


def main() -> None:
    log.info("worker starting")
    sched = build_scheduler()
    sched.start()
    # Run a morning ingest immediately on boot so a fresh install has data.
    try:
        jobs.job_morning()
    except Exception:
        log.exception("boot ingest failed")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        sched.shutdown()


if __name__ == "__main__":
    main()
