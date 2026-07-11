"""Alert Router (spec §6): decides which signals become Telegram alerts.

Send only when ALL hold:
  - the signal's action is in the caller's `allowed_actions` — the scheduler
    passes {"BUY"} after the 09:30 open scan and {"SELL"} after the 15:30
    close scan; those are the ONLY scans that alert (manual/chat runs pass
    nothing and never alert)
  - the risk engine approved (signal.actionable — there is no way around it)
  - confidence ≥ alert_confidence_threshold (default 0.80)
  - the daily rate limit (max_alerts_per_day, default 5) is not exhausted

Quiet hours affect notification delivery ONLY, never cost: the pipeline,
ingestion, and LLM budget decisions all happen upstream regardless of the
quiet-hours window. Every send attempt is recorded in the alerts table;
successful signal alerts flip signal.alert_sent.
"""

from collections.abc import Collection
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.alerts.format import format_signal_alert
from sentinel.alerts.telegram import send_telegram, telegram_configured
from sentinel.config import get_settings
from sentinel.db.models import AlertRow, Position
from sentinel.pipeline.persist import mark_alert_sent
from sentinel.pipeline.state import Signal

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")


def in_quiet_hours(db: Session, now: datetime | None = None) -> bool:
    """User-set quiet hours (Settings, ET). Signal alerts are suppressed and
    recorded as skipped; ops alerts still go through."""
    from sentinel.db.settings_store import QUIET_HOURS_KEY, get_setting

    window = get_setting(db, QUIET_HOURS_KEY)
    if not isinstance(window, dict) or "start" not in window or "end" not in window:
        return False
    hhmm = (now or datetime.now(ET)).astimezone(ET).strftime("%H:%M")
    start, end = window["start"], window["end"]
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end  # overnight window, e.g. 22:00–07:00


def alerts_sent_today(db: Session, kind: str = "signal") -> int:
    """Count successful alerts since ET midnight (the user's trading day)."""
    et_midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    since = et_midnight.astimezone(UTC)
    return int(
        db.execute(
            select(func.count(AlertRow.id)).where(
                AlertRow.kind == kind, AlertRow.ok.is_(True), AlertRow.ts >= since
            )
        ).scalar_one()
    )


def _estimated_sell_pnl(db: Session, signal: Signal) -> float | None:
    """SELL alerts state estimated realized P&L vs cost basis (spec §6)."""
    if signal.action != "SELL" or not signal.shares or signal.max_entry_price is None:
        return None
    position = db.get(Position, signal.ticker)
    if position is None:
        return None
    return round(
        (float(signal.max_entry_price) - float(position.cost_basis)) * signal.shares, 2
    )


def send_ops_alert(db: Session, text: str) -> bool:
    """Operational alert (spec §4.9 supervisor): stale data, pipeline or
    provider failures. Separate kind, not counted against the signal cap."""
    if not telegram_configured(db):
        return False
    ok, detail = send_telegram(db, text)
    db.add(AlertRow(kind="ops", ok=ok, text=text, detail=detail))
    db.flush()
    return ok


def route_signal_alerts(
    db: Session,
    signals: list[Signal],
    allowed_actions: Collection[str] = ("BUY", "SELL"),
) -> int:
    """Send alerts for qualifying signals; returns the number sent."""
    settings = get_settings()
    eligible = [
        s
        for s in signals
        if s.actionable
        and s.action in allowed_actions
        and s.confidence >= settings.alert_confidence_threshold
    ]
    if not eligible:
        return 0
    if not telegram_configured(db):
        log.info("alerts skipped: telegram not configured", eligible=len(eligible))
        return 0
    if in_quiet_hours(db):
        log.info("alerts suppressed: quiet hours", eligible=len(eligible))
        return 0

    sent = 0
    for signal in sorted(eligible, key=lambda s: s.confidence, reverse=True):
        if alerts_sent_today(db) + 1 > settings.max_alerts_per_day:
            log.warning(
                "alert rate limit reached",
                limit=settings.max_alerts_per_day,
                skipped=signal.ticker,
            )
            break
        text = format_signal_alert(signal, realized_pnl=_estimated_sell_pnl(db, signal))
        ok, detail = send_telegram(db, text)
        db.add(
            AlertRow(
                kind="signal",
                signal_id=str(signal.id),
                ok=ok,
                text=text,
                detail=detail,
            )
        )
        db.flush()
        if ok:
            signal.alert_sent = True
            mark_alert_sent(db, str(signal.id))
            sent += 1
    return sent
