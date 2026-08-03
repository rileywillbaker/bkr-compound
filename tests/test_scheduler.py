"""Scheduler contract: exactly three CORE scans per trading day (08:30 / 09:30
/ 15:30 ET), no rolling intraday core scan, everything mon-fri, and a hard
weekend guard inside the jobs themselves. The swing book adds its own two
scans (09:45 / 12:30 ET), and the Trend Discovery Agent its own two jobs
(07:45 collection / 09:50 report) — all also mon-fri and weekend-guarded."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sentinel.scheduler import jobs
from sentinel.scheduler.run import build_scheduler

ET = ZoneInfo("America/New_York")


def _fields(job) -> dict[str, str]:
    return {f.name: str(f) for f in job.trigger.fields}


@pytest.fixture()
def sched():
    # built but never started — nothing to shut down
    return build_scheduler()


def test_exactly_three_core_scans_and_no_intraday(sched):
    ids = {job.id for job in sched.get_jobs()}
    assert "intraday_scan" not in ids  # the 15/30-min rolling core scan is gone
    # The three core scans (unchanged) + support jobs. The swing book and the
    # trend agent add their own jobs separately (asserted below) without
    # touching these three.
    assert ids == {
        "premarket_discovery",
        "market_open_scan",
        "close_scan",
        "watchdog",
        "post_close",
        "nightly_eval",
        "swing_open",
        "swing_midday",
        "trend_collect",
        "trend_report",
    }


def test_trend_jobs_are_separate_and_correctly_ordered(sched):
    """Collection must run BEFORE the pre-market job so that job's discovery
    pass can consume today's trend snapshots; the report runs after the open
    scan, once prices have settled."""
    times = {j.id: _fields(j) for j in sched.get_jobs()}
    collect_at = (int(times["trend_collect"]["hour"]), int(times["trend_collect"]["minute"]))
    premarket_at = (
        int(times["premarket_discovery"]["hour"]),
        int(times["premarket_discovery"]["minute"]),
    )
    report_at = (int(times["trend_report"]["hour"]), int(times["trend_report"]["minute"]))
    open_scan_at = (
        int(times["market_open_scan"]["hour"]),
        int(times["market_open_scan"]["minute"]),
    )
    assert collect_at == (7, 45)
    assert collect_at < premarket_at
    assert report_at == (9, 50)
    assert report_at > open_scan_at


def test_swing_scans_are_separate(sched):
    times = {j.id: _fields(j) for j in sched.get_jobs()}
    assert (times["swing_open"]["hour"], times["swing_open"]["minute"]) == ("9", "45")
    assert (times["swing_midday"]["hour"], times["swing_midday"]["minute"]) == ("12", "30")


def test_scan_times_are_0830_0930_1530_et(sched):
    times = {
        job_id: (fields["hour"], fields["minute"])
        for job_id, fields in (
            (j.id, _fields(j)) for j in sched.get_jobs()
        )
    }
    assert times["premarket_discovery"] == ("8", "30")
    assert times["market_open_scan"] == ("9", "30")
    assert times["close_scan"] == ("15", "30")
    assert str(sched.timezone) == "America/New_York"


def test_no_job_can_fire_on_weekends(sched):
    for job in sched.get_jobs():
        assert _fields(job)["day_of_week"] == "mon-fri", job.id


class _FrozenDatetime(datetime):
    frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen


def _freeze(monkeypatch, when: datetime):
    _FrozenDatetime.frozen = when
    monkeypatch.setattr(jobs, "datetime", _FrozenDatetime)


def test_weekend_guard_blocks_saturday_and_sunday(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 11, 9, 30, tzinfo=ET))  # Saturday
    assert jobs._weekend_or_closed()
    _freeze(monkeypatch, datetime(2026, 7, 12, 9, 30, tzinfo=ET))  # Sunday
    assert jobs._weekend_or_closed()


def test_weekend_scan_jobs_do_nothing(monkeypatch):
    """On a Saturday no ingestion, no pipeline, no LLM, no alerts."""
    _freeze(monkeypatch, datetime(2026, 7, 11, 9, 30, tzinfo=ET))  # Saturday

    def boom(*args, **kwargs):
        raise AssertionError("must not run on a weekend")

    monkeypatch.setattr(jobs, "_run_pipeline_scan", boom)
    monkeypatch.setattr(jobs, "_send_brief", boom)
    monkeypatch.setattr(jobs, "_session", boom)
    jobs.job_premarket_discovery()
    jobs.job_market_open_scan()
    jobs.job_close_scan()
    jobs.job_post_close()
    jobs.job_watchdog()
    jobs.job_nightly_evaluation()
    jobs.job_swing_open()
    jobs.job_swing_midday()
    jobs.job_trend_collect()
    jobs.job_trend_report()


def test_open_scan_alerts_buy_only_close_scan_sell_only(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 8, 9, 30, tzinfo=ET))  # Wednesday
    monkeypatch.setattr(jobs, "is_trading_day", lambda: True)
    monkeypatch.setattr(jobs, "is_market_open", lambda: True)
    seen: list[frozenset] = []
    monkeypatch.setattr(
        jobs, "_run_pipeline_scan", lambda alert_actions: seen.append(alert_actions)
    )

    jobs.job_market_open_scan()
    assert seen == [frozenset({"BUY"})]

    seen.clear()
    monkeypatch.setattr(jobs, "_scan_symbols", lambda: ["NVDA"])

    class _NullSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(jobs, "_session", lambda: _NullSession())
    monkeypatch.setattr(jobs.ingest, "ingest_bars", lambda *a, **k: 0)
    monkeypatch.setattr(jobs.ingest, "ingest_quotes", lambda *a, **k: 0)
    jobs.job_close_scan()
    assert seen == [frozenset({"SELL"})]
