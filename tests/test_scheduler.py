"""Scheduler contract: exactly three scans per trading day (09:00 / 09:30 /
15:30 ET), no rolling intraday scan, everything mon-fri, and a hard weekend
guard inside the jobs themselves."""

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


def test_exactly_three_scans_and_no_intraday(sched):
    ids = {job.id for job in sched.get_jobs()}
    assert "intraday_scan" not in ids  # the 15/30-min rolling scan is gone
    assert ids == {
        "premarket_discovery",
        "market_open_scan",
        "close_scan",
        "watchdog",
        "post_close",
        "nightly_eval",
    }


def test_scan_times_are_0900_0930_1530_et(sched):
    times = {
        job_id: (fields["hour"], fields["minute"])
        for job_id, fields in (
            (j.id, _fields(j)) for j in sched.get_jobs()
        )
    }
    assert times["premarket_discovery"] == ("9", "0")
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
