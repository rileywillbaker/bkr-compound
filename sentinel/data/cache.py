"""Generic TTL cache over the `cache_entries` table.

The cheapest API call is the one you don't make. Two very different kinds of
waste are addressed here:

1. **Provider data that barely changes.** Sector, market cap, PE, 52-week
   high, earnings dates, SEC filing summaries, analyst ratings — refetching
   these daily for 700 tickers burns free-tier rate limit for nothing. Each
   gets a TTL and is only refreshed when it actually expires.
2. **Repeated AI analysis of an unchanged situation.** An LLM review is keyed
   by a *fingerprint* of the deterministic facts that drove it (rounded
   indicators, strategy, action, regime, news/earnings state). If nothing
   material moved, the stored review is reused for free. This is what stops
   the same NVDA setup being re-explained three times a day.

Entries are rows, not process memory, so a container restart (or the host
waking from sleep — see the scheduler's hardening) keeps the savings.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sentinel.db.models import CacheEntry

log = structlog.get_logger()

# ---------------------------------------------------------------- TTLs ----
# Tuned to how fast each fact actually changes, not to how often we could ask.
TTL_COMPANY_PROFILE = timedelta(days=30)  # sector, exchange, name
TTL_FUNDAMENTALS = timedelta(days=7)  # PE/PS/beta/52w — weekly is plenty
TTL_MARKET_CAP = timedelta(days=7)
TTL_EARNINGS_CALENDAR = timedelta(hours=20)  # once per trading day
TTL_ANALYST_RATINGS = timedelta(days=7)
TTL_FILING_SUMMARY = timedelta(days=90)  # a filed document never changes
TTL_SHORT_INTEREST = timedelta(days=3)  # published semi-monthly
TTL_LLM_REVIEW = timedelta(hours=72)  # bounded even if facts look identical


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(ts: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat stored values as UTC."""
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def fingerprint(payload: Any) -> str:
    """Stable short hash of any JSON-able structure.

    Callers should round/bucket noisy floats BEFORE fingerprinting — the point
    is to detect *material* change, not the third decimal of an EMA.
    """
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def cache_key(kind: str, *parts: str) -> str:
    return ":".join([kind, *[str(p) for p in parts]])


def cache_get(db: Session, key: str) -> Any | None:
    """Payload for a live entry, or None when missing/expired."""
    row = db.get(CacheEntry, key)
    if row is None:
        return None
    expires = _aware(row.expires_at)
    if expires is not None and expires <= _now():
        return None
    return row.payload


def cache_set(db: Session, key: str, payload: Any, ttl: timedelta, kind: str = "") -> None:
    now = _now()
    row = db.get(CacheEntry, key)
    if row is None:
        db.add(
            CacheEntry(
                key=key,
                kind=kind,
                payload=payload,
                created_at=now,
                expires_at=now + ttl,
            )
        )
    else:
        row.kind = kind or row.kind
        row.payload = payload
        row.created_at = now
        row.expires_at = now + ttl
    db.flush()


def is_fresh(db: Session, key: str) -> bool:
    return cache_get(db, key) is not None


def mark_fresh(db: Session, key: str, ttl: timedelta, kind: str = "") -> None:
    """Record 'this was fetched just now' without storing a payload.

    Used by ingestion jobs whose real output lands in a domain table (bars,
    fundamentals, filings) — the cache row is purely the freshness marker that
    lets the next run skip the provider call.
    """
    cache_set(db, key, {"fetched_at": _now().isoformat()}, ttl, kind=kind or "freshness")


def purge_expired(db: Session, limit: int = 5000) -> int:
    """Drop expired rows. Cheap housekeeping for the nightly job."""
    stale = db.execute(
        select(CacheEntry.key).where(CacheEntry.expires_at <= _now()).limit(limit)
    ).scalars().all()
    if not stale:
        return 0
    db.execute(delete(CacheEntry).where(CacheEntry.key.in_(stale)))
    db.flush()
    return len(stale)


def stats(db: Session) -> dict:
    """Cache size/health for the System view."""
    rows = db.execute(select(CacheEntry.kind, CacheEntry.expires_at)).all()
    now = _now()
    by_kind: dict[str, int] = {}
    live = 0
    for kind, expires in rows:
        by_kind[kind or "(unspecified)"] = by_kind.get(kind or "(unspecified)", 0) + 1
        exp = _aware(expires)
        if exp is None or exp > now:
            live += 1
    return {"entries": len(rows), "live": live, "expired": len(rows) - live, "by_kind": by_kind}
