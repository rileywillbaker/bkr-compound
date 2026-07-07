"""Alert message formatting — exact spec §6 format, no user math required.

Every message ends with the not-financial-advice line (hard rule: disclaimers
on every alert). All numbers arrive pre-computed; nothing is derived here
beyond string formatting.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sentinel.pipeline.state import Signal

ET = ZoneInfo("America/New_York")

FOOTER = "Not financial advice. You place all trades."

_HORIZON_LABELS = {
    "intraday": "intraday (0–1d)",
    "swing_days": "swing (3–10d)",
    "position_weeks": "position (2–8w)",
    "long_term": "long-term (3m+)",
}


def _et_stamp(ts: datetime) -> str:
    local = ts.astimezone(ET)
    return local.strftime("%I:%M %p ET").lstrip("0")


def _money(value) -> str:
    return f"${float(value):,.2f}"


def format_signal_alert(signal: Signal, realized_pnl: float | None = None) -> str:
    """BUY/SELL alert per the spec's exact template. For SELL, realized_pnl
    is the estimated P&L in dollars at the signal's price (computed by the
    caller from cost basis)."""
    emoji = "🟢" if signal.action == "BUY" else "🔴"
    lines = [f"{emoji} {signal.action} ALERT — {signal.ticker}"]
    lines.append(f"Shares: {signal.shares}")
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
    if signal.action == "SELL" and realized_pnl is not None:
        lines.append(f"Est. P&L: {'+' if realized_pnl >= 0 else '−'}{_money(abs(realized_pnl))}")
    if signal.expected_return_pct is not None:
        lines.append(f"Expected: {signal.expected_return_pct:+.1f}%")
    lines.append(f"Why: {signal.explanation}")
    lines.append(f"{_et_stamp(signal.created_at)} — {FOOTER}")
    return "\n".join(lines)


def format_brief(kind: str, body_lines: list[str], now: datetime | None = None) -> str:
    title = {
        "pre_open": "☀️ Pre-open brief",
        "post_close": "🌙 Post-close recap",
    }.get(kind, kind)
    stamp = (now or datetime.now(UTC)).astimezone(ET).strftime("%a %b %d")
    lines = [f"{title} — {stamp}", *body_lines, FOOTER]
    return "\n".join(lines)
