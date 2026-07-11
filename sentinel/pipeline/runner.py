"""Pipeline entry point. The scheduler's three daily scans call run_scan; the
/api/pipeline router and chat call it for on-demand runs. Each run persists
its signals, routes qualifying ones to the alert channel, and is recorded to
the system_events audit trail; the last result is kept in memory for the API.

Symbols default to the day's scan set — discovery candidates + highlighted
watchlist + held positions — never the watchlist alone; any ticker in the
expanded universe (or passed explicitly) goes through the identical graph,
ending in the risk gate. `alert_actions` gates which signal actions may
alert: the 09:30 scan passes {"BUY"}, the 15:30 scan passes {"SELL"}, and
on-demand runs default to none (signals are still persisted and visible)."""

import contextlib
from collections.abc import Collection
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from sentinel.alerts.router import route_signal_alerts, send_ops_alert
from sentinel.data.bus import publish
from sentinel.data.discovery import get_scan_symbols
from sentinel.db.models import SystemEvent
from sentinel.pipeline.graph import build_graph
from sentinel.pipeline.persist import save_signals
from sentinel.pipeline.state import PipelineState

log = structlog.get_logger()

_last_run: PipelineState | None = None


def last_run() -> PipelineState | None:
    return _last_run


def run_scan(
    db: Session,
    symbols: list[str] | None = None,
    use_llm: bool = True,
    alert_actions: Collection[str] = (),
) -> PipelineState:
    global _last_run
    symbols = symbols or get_scan_symbols(db)
    initial = PipelineState(symbols=symbols, use_llm=use_llm)
    graph = build_graph(db)
    try:
        result = PipelineState.model_validate(graph.invoke(initial))
    except Exception as exc:
        db.add(
            SystemEvent(
                level="ERROR",
                kind="pipeline.failed",
                message=str(exc)[:500],
                payload={"run_id": str(initial.run_id), "symbols": symbols},
            )
        )
        db.flush()
        log.exception("pipeline run failed", run_id=str(initial.run_id))
        # never let alerting mask the original failure
        with contextlib.suppress(Exception):
            send_ops_alert(db, f"⚠️ B-Quant pipeline run failed: {str(exc)[:200]}")
        raise
    save_signals(db, result)
    alerts_sent = route_signal_alerts(db, result.signals, allowed_actions=alert_actions)
    for signal in result.signals:
        with contextlib.suppress(Exception):
            publish(
                "signal",
                {
                    "id": str(signal.id),
                    "ticker": signal.ticker,
                    "action": signal.action,
                    "confidence": signal.confidence,
                    "strategy": signal.strategy,
                    "regime": signal.regime,
                    "approved": bool(signal.risk_check and signal.risk_check.approved),
                    "deterministic_only": signal.deterministic_only,
                },
            )
    actionable = [s for s in result.signals if s.actionable]
    rejected = [
        s
        for s in result.signals
        if s.action in ("BUY", "SELL") and s.risk_check and not s.risk_check.approved
    ]
    db.add(
        SystemEvent(
            kind="pipeline.run",
            message=f"scan of {len(symbols)} symbols: "
            f"{len(result.signals)} signals, {len(actionable)} actionable, "
            f"{len(rejected)} vetoed, {alerts_sent} alerted",
            payload={
                "run_id": str(result.run_id),
                "alert_actions": sorted(alert_actions),
                "regime": result.regime.regime if result.regime else None,
                "duration_seconds": (datetime.now(UTC) - result.started_at).total_seconds(),
                "signals": [str(s.id) for s in result.signals],
            },
        )
    )
    db.flush()
    _last_run = result
    return result
