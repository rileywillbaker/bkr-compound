"""Swing alert channel: SWING-labeled Telegram messages with their OWN daily
cap, kept separate from the core signal cap so a busy swing day never starves
the long-term BUY/SELL alerts (and vice-versa).

Everything else is identical to the core router and reused from it: the same
confidence threshold, the same absolute requirement that the risk engine
approved (`signal.actionable`), the same quiet-hours suppression, the same
telegram channel. Nothing is loosened. Every send is logged to `alerts` with
kind="swing_signal" (its own rate-limit bucket) and ends with the disclaimer.
"""

from collections.abc import Collection

import structlog
from sqlalchemy.orm import Session

from sentinel.alerts.format import _HORIZON_LABELS, FOOTER, _et_stamp, _money
from sentinel.alerts.router import alerts_sent_today, in_quiet_hours
from sentinel.alerts.telegram import send_telegram, telegram_configured
from sentinel.config import get_settings
from sentinel.db.models import AlertRow
from sentinel.pipeline.persist import mark_alert_sent
from sentinel.pipeline.state import Signal

log = structlog.get_logger()

SWING_ALERT_KIND = "swing_signal"
DEFAULT_SWING_ALERT_CAP = 3


def format_swing_alert(signal: Signal) -> str:
    """SWING BUY/SELL alert. Same layout as the core alert with a SWING label
    so the two are visually distinct in Telegram. Ends with the disclaimer."""
    emoji = "🟢" if signal.action == "BUY" else "🔴"
    lines = [f"{emoji} SWING {signal.action} — {signal.ticker}", f"Shares: {signal.shares}"]
    if signal.action == "BUY":
        if signal.max_entry_price is not None:
            lines.append(f"Max Price: {_money(signal.max_entry_price)}")
        if signal.stop_loss is not None:
            lines.append(f"Stop Loss: {_money(signal.stop_loss)}")
        if signal.take_profit is not None:
            lines.append(f"Target: {_money(signal.take_profit)}")
    horizon = _HORIZON_LABELS.get(signal.time_horizon, signal.time_horizon)
    lines.append(
        f"Confidence: {signal.confidence:.0%} | Risk: {signal.risk_score}/10 | "
        f"Horizon: {horizon}"
    )
    if signal.expected_return_pct is not None:
        lines.append(f"Expected: {signal.expected_return_pct:+.1f}%")
    lines.append(f"Why: {signal.explanation}")
    lines.append(f"{_et_stamp(signal.created_at)} — {FOOTER}")
    return "\n".join(lines)


def route_swing_alerts(
    db: Session,
    signals: list[Signal],
    allowed_actions: Collection[str] = ("BUY", "SELL"),
) -> int:
    """Send swing alerts for qualifying signals; returns the number sent.

    Qualifies only when risk-approved AND confidence ≥ threshold AND action is
    allowed AND the swing daily cap is not exhausted."""
    settings = get_settings()
    cap = getattr(settings, "swing_max_alerts_per_day", DEFAULT_SWING_ALERT_CAP)
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
        log.info("swing alerts skipped: telegram not configured", eligible=len(eligible))
        return 0
    if in_quiet_hours(db):
        log.info("swing alerts suppressed: quiet hours", eligible=len(eligible))
        return 0

    sent = 0
    for signal in sorted(eligible, key=lambda s: s.confidence, reverse=True):
        if alerts_sent_today(db, kind=SWING_ALERT_KIND) + 1 > cap:
            log.warning("swing alert cap reached", cap=cap, skipped=signal.ticker)
            break
        text = format_swing_alert(signal)
        ok, detail = send_telegram(db, text)
        db.add(
            AlertRow(
                kind=SWING_ALERT_KIND,
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
