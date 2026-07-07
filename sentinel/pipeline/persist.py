"""Signal persistence + user decision capture (spec §10 Phase 4).

Signals are written once, after the risk gate. The only mutable fields are
alert_sent (set by the alert router) and user_decision/decided_at (set by the
user); everything the pipeline computed is immutable history.
"""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import JournalEntryRow, RiskCheckRow, SignalRow
from sentinel.pipeline.state import PipelineState

Decision = Literal["taken", "skipped", "modified"]


def save_signals(db: Session, state: PipelineState) -> list[SignalRow]:
    rows = []
    for signal in state.signals:
        row = SignalRow(
            id=str(signal.id),
            created_at=signal.created_at,
            run_id=str(state.run_id),
            ticker=signal.ticker,
            action=signal.action,
            shares=signal.shares,
            max_entry_price=signal.max_entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            confidence=signal.confidence,
            expected_return_pct=signal.expected_return_pct,
            risk_score=signal.risk_score,
            time_horizon=signal.time_horizon,
            strategy=signal.strategy,
            regime=signal.regime,
            evidence=[e.model_dump(mode="json") for e in signal.evidence],
            explanation=signal.explanation,
            deterministic_only=signal.deterministic_only,
            alert_sent=False,
            user_decision=None,
        )
        db.add(row)
        if signal.risk_check is not None:
            db.add(
                RiskCheckRow(
                    signal_id=str(signal.id),
                    approved=signal.risk_check.approved,
                    profile_version=signal.risk_check.profile_version,
                    checked_at=signal.risk_check.checked_at,
                    rules=[r.model_dump(mode="json") for r in signal.risk_check.rules],
                )
            )
        rows.append(row)
    db.flush()
    return rows


def get_signal(db: Session, signal_id: str) -> SignalRow | None:
    return db.get(SignalRow, signal_id)


def get_risk_check(db: Session, signal_id: str) -> RiskCheckRow | None:
    return db.execute(
        select(RiskCheckRow).where(RiskCheckRow.signal_id == signal_id)
    ).scalars().first()


def list_signals(
    db: Session,
    ticker: str | None = None,
    action: str | None = None,
    decision: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[SignalRow]:
    query = select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
    if ticker:
        query = query.where(SignalRow.ticker == ticker.upper())
    if action:
        query = query.where(SignalRow.action == action.upper())
    if decision:
        query = query.where(SignalRow.user_decision == decision)
    if since:
        query = query.where(SignalRow.created_at >= since)
    return list(db.execute(query).scalars().all())


def record_decision(
    db: Session, signal_id: str, decision: Decision, note: str = ""
) -> SignalRow | None:
    """Capture the user's decision and auto-create the journal entry."""
    row = db.get(SignalRow, signal_id)
    if row is None:
        return None
    row.user_decision = decision
    row.decided_at = datetime.now(UTC)
    db.add(
        JournalEntryRow(
            signal_id=signal_id, ticker=row.ticker, decision=decision, note=note
        )
    )
    db.flush()
    return row


def mark_alert_sent(db: Session, signal_id: str) -> None:
    row = db.get(SignalRow, signal_id)
    if row is not None:
        row.alert_sent = True
        db.flush()
