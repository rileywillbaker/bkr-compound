"""Render the daily B-Quant Trend Report and send it.

The message follows the requested layout exactly — market environment, top
emerging trends with a strength score and a reason, then the best
opportunities in DOLLARS. Formatting only: every number arrives already
computed by `agent.py`, which got it from the risk engine and the position
sizer. Nothing is derived here.

Three things the format insists on, because they are what make the report
safe to act on:

* **Dollars, not percentages.** "$35 of UEC" is a decision; "1.4% of the book"
  is homework.
* **The bear case is never omitted.** Every recommendation carries its
  bearish points and its risk level, not just the pitch.
* **Refusals are shown.** Names the risk engine declined, and names excluded
  as possible pumps, appear with the reason. A report that only ever shows
  what it liked teaches the user nothing about what it rejects.

Every message ends with the standard disclaimer, per the project's hard rule.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.orm import Session

from sentinel.alerts.format import FOOTER
from sentinel.alerts.telegram import send_telegram, telegram_configured
from sentinel.db.models import AlertRow
from sentinel.trends.agent import Opportunity, TrendReport

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")

_LEGITIMACY_LABEL = {
    "legitimate": "confirmed",
    "emerging": "emerging",
    "mixed": "mixed evidence",
    "hype": "looks like hype",
    "unproven": "unproven",
}


def _money(value: float) -> str:
    return f"${value:,.0f}" if abs(value) >= 100 else f"${value:,.2f}"


def _opportunity_block(opportunity: Opportunity, index: int) -> list[str]:
    allocation = opportunity.allocation
    lines = [
        "",
        f"{index}. {opportunity.symbol} — {opportunity.company}",
    ]
    if opportunity.price is not None:
        lines.append(f"Current Price: {_money(opportunity.price)}")
    lines.append(f"Theme: {opportunity.theme_name} ({opportunity.theme_score:.0f}/100)")
    lines.append(f"Trend Connection: {opportunity.trend_connection}")
    if opportunity.bullish:
        lines.append("Bullish: " + "; ".join(opportunity.bullish[:3]))
    if opportunity.bearish:
        lines.append("Bearish: " + "; ".join(opportunity.bearish[:3]))
    lines.append(
        f"Risk Level: {opportunity.risk_level} | "
        f"Confidence Score: {opportunity.confidence:.0%}"
    )
    if allocation is not None and allocation.approved:
        note = " (fractional shares required)" if allocation.fractional_required else (
            f" ({allocation.shares} share{'s' if allocation.shares != 1 else ''})"
        )
        lines.append(f"Purchase Amount: {_money(allocation.dollars)}{note}")
        if allocation.stop_loss is not None:
            lines.append(f"Stop Loss: {_money(allocation.stop_loss)}")
        if allocation.take_profit is not None:
            lines.append(f"Target: {_money(allocation.take_profit)}")
    return lines


def compose(report: TrendReport) -> str:
    """Render the full report as the daily message."""
    stamp = report.generated_at.astimezone(ET).strftime("%a %b %d")
    lines = [f"📈 B-Quant Trend Report — {stamp}", ""]

    lines.append(f"Market Environment: {report.market_environment}")
    if report.market_environment_detail:
        lines.append(f"  {report.market_environment_detail}")

    # --- trends -----------------------------------------------------------
    lines.append("")
    if report.trends:
        lines.append("Top Emerging Trends:")
        for index, trend in enumerate(report.trends, start=1):
            label = _LEGITIMACY_LABEL.get(trend.legitimacy, trend.legitimacy)
            lines.append("")
            lines.append(f"{index}. {trend.name}")
            lines.append(f"Strength Score: {trend.score:.0f}/100 ({label})")
            lines.append(f"Reason: {trend.explanation}")
            if trend.hype_flags:
                lines.append("Caution: " + "; ".join(trend.hype_flags[:2]))
    else:
        lines.append("Top Emerging Trends: none scored high enough today.")

    # --- opportunities ----------------------------------------------------
    lines.append("")
    actionable = report.actionable
    if actionable:
        lines.append("Best Opportunities:")
        for index, opportunity in enumerate(actionable, start=1):
            lines.extend(_opportunity_block(opportunity, index))
            lines.append(f"Why: {opportunity.why_selected}")
        lines.append("")
        lines.append("Suggested Buys:")
        for opportunity in actionable:
            allocation = opportunity.allocation
            if allocation is None:
                continue
            lines.append(
                f"  BUY {opportunity.symbol} — {_money(allocation.dollars)} — "
                f"Risk: {opportunity.risk_level}"
            )
        total = sum(o.dollars for o in actionable)
        lines.append(f"  Total proposed: {_money(total)}")
    else:
        lines.append(
            "Best Opportunities: none. No name cleared the quality gate, the "
            "pump filter and the risk engine at an actionable size today."
        )

    # --- what was refused, and why ---------------------------------------
    if report.rejected:
        lines.append("")
        lines.append("Considered but not recommended:")
        for opportunity in report.rejected[:4]:
            reason = (
                opportunity.allocation.summary
                if opportunity.allocation
                else "no allocation computed"
            )
            lines.append(f"  {opportunity.symbol}: {reason}")

    pumps = [s for s in report.excluded_stocks if "pump" in s.exclusion_reason]
    if pumps:
        lines.append("")
        lines.append("Excluded as possible pumps:")
        for stock in pumps[:3]:
            lines.append(f"  {stock.symbol}: {stock.exclusion_reason}")

    # --- portfolio context ------------------------------------------------
    portfolio = report.portfolio or {}
    if portfolio:
        lines.append("")
        lines.append(
            f"Portfolio: equity {_money(portfolio.get('equity', 0))}, "
            f"cash {_money(portfolio.get('cash', 0))}, "
            f"{portfolio.get('open_positions', 0)}/{portfolio.get('max_open_positions', 0)} "
            "positions open"
        )
        urgent = [r for r in report.position_reviews if r.urgency >= 4]
        for review in urgent[:3]:
            lines.append(f"  {review.action} {review.symbol}: {review.reasons[0] if review.reasons else ''}")

    # --- honesty about coverage ------------------------------------------
    gaps = (report.coverage or {}).get("components_unmeasured") or {}
    if gaps:
        lines.append("")
        lines.append(
            "Not measured today (free source unavailable): "
            + ", ".join(sorted(gaps))
        )
    for note in report.notes:
        lines.append(f"Note: {note}")

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def send_report(db: Session, report: TrendReport, persist_text: bool = True) -> str | None:
    """Compose, store and send the report. Returns the text.

    Returns the text even when Telegram is unconfigured (nothing is sent in
    that case) so the API and the UI always have something to show.
    """
    text = compose(report)

    if persist_text:
        from sentinel.trends.agent import latest_report

        row = latest_report(db)
        if row is not None and row.day == report.day:
            row.text = text

    if not telegram_configured(db):
        log.info("trend report composed but telegram is not configured")
        db.flush()
        return text

    ok, detail = send_telegram(db, text)
    db.add(AlertRow(kind="trend_report", ok=ok, text=text, detail=detail))
    if persist_text:
        from sentinel.trends.agent import latest_report

        row = latest_report(db)
        if row is not None and row.day == report.day:
            row.alert_sent = ok
    db.flush()
    log.info("trend report sent", ok=ok)
    return text


def compose_and_send(db: Session, report: TrendReport) -> str | None:
    return send_report(db, report)
