"""TTL cache: expiry, fingerprint stability, freshness markers, purging."""

from datetime import UTC, datetime, timedelta

from sentinel.data.cache import (
    cache_get,
    cache_key,
    cache_set,
    fingerprint,
    is_fresh,
    mark_fresh,
    purge_expired,
    stats,
)
from sentinel.db.models import CacheEntry


def test_set_then_get_round_trip(db):
    cache_set(db, "k1", {"a": 1}, timedelta(hours=1), kind="test")
    assert cache_get(db, "k1") == {"a": 1}


def test_missing_key_is_none(db):
    assert cache_get(db, "nope") is None


def test_expired_entry_is_not_served(db):
    cache_set(db, "k1", {"a": 1}, timedelta(hours=1), kind="test")
    row = db.get(CacheEntry, "k1")
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    assert cache_get(db, "k1") is None
    assert not is_fresh(db, "k1")


def test_set_overwrites_and_extends(db):
    cache_set(db, "k1", {"a": 1}, timedelta(seconds=1), kind="test")
    cache_set(db, "k1", {"a": 2}, timedelta(hours=1), kind="test")
    assert cache_get(db, "k1") == {"a": 2}
    assert db.query(CacheEntry).count() == 1


def test_freshness_marker_gates_a_refetch(db):
    key = cache_key("fundamentals", "NVDA")
    assert not is_fresh(db, key)
    mark_fresh(db, key, timedelta(days=7), kind="fundamentals")
    assert is_fresh(db, key)


def test_fingerprint_is_stable_and_order_independent():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert len(fingerprint({"a": 1})) == 32


def test_purge_removes_only_expired(db):
    cache_set(db, "live", {"x": 1}, timedelta(hours=1), kind="test")
    cache_set(db, "dead", {"x": 1}, timedelta(hours=1), kind="test")
    db.get(CacheEntry, "dead").expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    assert purge_expired(db) == 1
    assert cache_get(db, "live") == {"x": 1}
    assert db.get(CacheEntry, "dead") is None


def test_stats_reports_live_and_expired(db):
    cache_set(db, "a", {}, timedelta(hours=1), kind="llm_review")
    cache_set(db, "b", {}, timedelta(hours=1), kind="fundamentals")
    db.get(CacheEntry, "b").expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    result = stats(db)
    assert result["entries"] == 2
    assert result["live"] == 1
    assert result["expired"] == 1
    assert result["by_kind"]["llm_review"] == 1
