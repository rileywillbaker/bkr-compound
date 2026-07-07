"""Analytics summary (spec §7.6).

Signal-level aggregates are available now; outcome-based stats (hit rate,
expectancy, Sharpe/Sortino, Brier calibration) need resolved signals from the
Phase 6 evaluation loop and report as pending until it lands and 30+ signals
resolve."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.db.base import get_db
from sentinel.db.models import SignalRow

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def analytics_summary(db: Session) -> dict:
    total = db.execute(select(func.count()).select_from(SignalRow)).scalar_one()
    by_action: dict[str, int] = {
        row[0]: row[1]
        for row in db.execute(
            select(SignalRow.action, func.count()).group_by(SignalRow.action)
        ).all()
    }
    by_strategy: dict[str, int] = {
        row[0]: row[1]
        for row in db.execute(
            select(SignalRow.strategy, func.count()).group_by(SignalRow.strategy)
        ).all()
    }
    by_regime: dict[str, int] = {
        row[0]: row[1]
        for row in db.execute(
            select(SignalRow.regime, func.count()).group_by(SignalRow.regime)
        ).all()
    }
    by_decision: dict[str, int] = {
        str(row[0]): row[1]
        for row in db.execute(
            select(SignalRow.user_decision, func.count())
            .where(SignalRow.user_decision.is_not(None))
            .group_by(SignalRow.user_decision)
        ).all()
    }
    return {
        "signals_total": int(total),
        "by_action": by_action,
        "by_strategy": by_strategy,
        "by_regime": by_regime,
        "by_decision": by_decision,
        "resolved": {
            "count": 0,
            "hit_rate": None,
            "expectancy_r": None,
            "sharpe": None,
            "sortino": None,
            "brier_score": None,
            "calibration": [],
            "note": "outcome stats activate once the nightly evaluation loop "
            "(Phase 6) has resolved signals; neutral priors until 30+ resolve",
        },
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    return {**analytics_summary(db), "disclaimer": DISCLAIMER}
