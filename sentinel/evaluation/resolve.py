"""Nightly signal resolution (spec §9).

A BUY signal resolves when its stop, target, or horizon expiry is hit against
actual daily bars — including signals the user skipped, so missed
opportunities and false positives are both measured.

Fill model (deterministic, conservative):
  - entry is assumed at max_entry_price at signal time;
  - bars from the signal's creation day onward are walked in order;
  - if a bar's range spans BOTH stop and target, the stop is assumed to hit
    first (pessimistic tie-break);
  - past the horizon's trading-day budget the position exits at that bar's
    close ("expired").

This bar-walk is also the paper-fill sanity harness: no brokerage order
endpoint exists anywhere in this codebase, live or paper.
"""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import BarRow, EvaluationRow, SignalRow, SystemEvent

log = structlog.get_logger()

# Trading-day budget per horizon (spec §4 time_horizon values).
HORIZON_TRADING_DAYS = {
    "intraday": 1,
    "swing_days": 10,
    "position_weeks": 40,
    "long_term": 120,
}


def _unresolved_buy_signals(db: Session) -> list[SignalRow]:
    resolved_ids = select(EvaluationRow.signal_id)
    query = (
        select(SignalRow)
        .where(
            SignalRow.action == "BUY",
            SignalRow.shares.is_not(None),
            SignalRow.max_entry_price.is_not(None),
            SignalRow.stop_loss.is_not(None),
            SignalRow.take_profit.is_not(None),
            SignalRow.id.not_in(resolved_ids),
        )
        .order_by(SignalRow.created_at)
    )
    return list(db.execute(query).scalars().all())


def _bars_since(db: Session, ticker: str, since: datetime) -> list[BarRow]:
    day_start = since.replace(hour=0, minute=0, second=0, microsecond=0)
    return list(
        db.execute(
            select(BarRow)
            .where(
                BarRow.symbol == ticker,
                BarRow.timeframe == "1Day",
                BarRow.ts >= day_start,
            )
            .order_by(BarRow.ts)
        ).scalars().all()
    )


def resolve_signal(db: Session, signal: SignalRow) -> EvaluationRow | None:
    """Resolve one signal against stored bars; None if still open."""
    entry = float(signal.max_entry_price)  # type: ignore[arg-type]
    stop = float(signal.stop_loss)  # type: ignore[arg-type]
    target = float(signal.take_profit)  # type: ignore[arg-type]
    risk = entry - stop
    if risk <= 0:  # defensive: sizing guarantees stop < entry for BUY
        log.warning("signal has non-positive risk; skipping", signal_id=signal.id)
        return None

    budget = HORIZON_TRADING_DAYS.get(signal.time_horizon, 10)
    bars = _bars_since(db, signal.ticker, signal.created_at)

    outcome: str | None = None
    exit_price = entry
    holding_days = 0
    for i, bar in enumerate(bars, start=1):
        holding_days = i
        if float(bar.low) <= stop:  # pessimistic: stop checked first
            outcome, exit_price = "stop_hit", stop
            break
        if float(bar.high) >= target:
            outcome, exit_price = "target_hit", target
            break
        if i >= budget:
            outcome, exit_price = "expired", float(bar.close)
            break
    if outcome is None:
        return None  # horizon not exhausted and no exit level touched yet

    r_multiple = round((exit_price - entry) / risk, 4)
    evaluation = EvaluationRow(
        signal_id=signal.id,
        ticker=signal.ticker,
        resolved_at=datetime.now(UTC),
        outcome=outcome,
        entry_price=entry,
        exit_price=exit_price,
        r_multiple=r_multiple,
        return_pct=round((exit_price - entry) / entry * 100, 4),
        win=r_multiple > 0,
        holding_days=holding_days,
        confidence=signal.confidence,
        strategy=signal.strategy,
        regime=signal.regime,
        user_decision=signal.user_decision,
    )
    db.add(evaluation)
    db.flush()
    return evaluation


def resolve_open_signals(db: Session) -> list[EvaluationRow]:
    resolved = []
    for signal in _unresolved_buy_signals(db):
        evaluation = resolve_signal(db, signal)
        if evaluation is not None:
            resolved.append(evaluation)
    return resolved


def run_nightly(db: Session) -> dict:
    """Scheduler entrypoint (02:00 ET): resolve, then refresh aggregates."""
    from sentinel.evaluation.stats import recompute_strategy_stats

    resolved = resolve_open_signals(db)
    stats = recompute_strategy_stats(db)
    db.add(
        SystemEvent(
            kind="evaluation.nightly",
            message=f"resolved {len(resolved)} signals; {len(stats)} strategy stats rows",
            payload={
                "resolved": [
                    {"signal_id": e.signal_id, "outcome": e.outcome, "r": e.r_multiple}
                    for e in resolved
                ]
            },
        )
    )
    db.flush()
    return {"resolved": len(resolved), "stats_rows": len(stats)}
