"""Daily pre-open brief and post-close recap (spec §6).

Assembled deterministically from the database — no LLM. The scheduler's
08:30/16:45 ET jobs call send_brief; the composed text is returned so tests
(and the API) can inspect it without a Telegram round trip.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.agents.regime import classify_regime
from sentinel.alerts.format import format_brief
from sentinel.alerts.telegram import send_telegram, telegram_configured
from sentinel.config import get_settings
from sentinel.data.context import build_market_context
from sentinel.db.models import AlertRow, SignalRow
from sentinel.portfolio.state import build_portfolio_state

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")


def _positions_lines(db: Session) -> list[str]:
    state = build_portfolio_state(db)
    lines = [f"Equity: ${state.equity:,.2f} (day P&L {state.day_pnl:+,.2f})"]
    if state.positions:
        for p in state.positions:
            lines.append(f"  {p.symbol}: {p.shares} @ ${p.price:,.2f}")
    else:
        lines.append("  no open positions")
    return lines


def compose_pre_open(db: Session) -> str:
    symbols = get_settings().watchlist_symbols
    context = build_market_context(db, symbols)
    regime = classify_regime(context.spy_bars, context.macro.get("VIXCLS"))
    lines = [f"Regime: {regime.regime} — {regime.detail}"]
    watch = []
    for symbol, sym_ctx in context.symbols.items():
        if sym_ctx.daily_bars:
            watch.append(f"{symbol} ${float(sym_ctx.daily_bars[-1].close):,.2f}")
    if watch:
        lines.append("Watchlist: " + ", ".join(watch))
    lines.extend(_positions_lines(db))
    return format_brief("pre_open", lines)


def compose_post_close(db: Session) -> str:
    since = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    signals = (
        db.execute(
            select(SignalRow).where(SignalRow.created_at >= since.astimezone(UTC))
        ).scalars().all()
    )
    actionable = [s for s in signals if s.action in ("BUY", "SELL")]
    lines = _positions_lines(db)
    lines.append(f"Signals today: {len(signals)} ({len(actionable)} BUY/SELL)")
    for s in actionable:
        decision = s.user_decision or "pending"
        lines.append(f"  {s.action} {s.ticker} conf {s.confidence:.0%} — {decision}")
    return format_brief("post_close", lines)


def send_brief(db: Session, kind: str) -> str | None:
    """Compose and send the brief; records the attempt. Returns the text
    (None when telegram is unconfigured — nothing is sent or recorded)."""
    if kind == "pre_open":
        text = compose_pre_open(db)
    elif kind == "post_close":
        text = compose_post_close(db)
    else:
        raise ValueError(f"unknown brief kind: {kind}")
    if not telegram_configured(db):
        log.info("brief skipped: telegram not configured", kind=kind)
        return None
    ok, detail = send_telegram(db, text)
    db.add(AlertRow(kind=f"brief_{kind}", ok=ok, text=text, detail=detail))
    db.flush()
    return text


def recent_alerts(db: Session, days: int = 7, limit: int = 100) -> list[AlertRow]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return list(
        db.execute(
            select(AlertRow)
            .where(AlertRow.ts >= cutoff)
            .order_by(AlertRow.ts.desc())
            .limit(limit)
        ).scalars().all()
    )
