"""Aggregate statistics over resolved signals (spec §9): per-strategy and
per-regime hit rate + expectancy, Brier score for confidence calibration,
calibration buckets, and Sharpe/Sortino over resolved returns.

These stats feed the synthesizer's confidence prior and the analytics view.
Risk limits are NEVER derived from them — those change only via explicit
user edits in Settings.
"""

import math
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sentinel.db.models import EvaluationRow, StrategyStatRow


def recompute_strategy_stats(db: Session) -> list[StrategyStatRow]:
    """Rebuild strategy_stats from evaluations: one row per (strategy, regime)
    pair plus a regime='*' rollup per strategy."""
    evaluations = list(db.execute(select(EvaluationRow)).scalars().all())
    groups: dict[tuple[str, str], list[EvaluationRow]] = {}
    for e in evaluations:
        groups.setdefault((e.strategy, e.regime), []).append(e)
        groups.setdefault((e.strategy, "*"), []).append(e)

    db.execute(delete(StrategyStatRow))
    rows = []
    for (strategy, regime), members in sorted(groups.items()):
        wins = sum(1 for e in members if e.win)
        row = StrategyStatRow(
            strategy=strategy,
            regime=regime,
            resolved_count=len(members),
            wins=wins,
            hit_rate=round(wins / len(members), 4),
            expectancy_r=round(sum(e.r_multiple for e in members) / len(members), 4),
            updated_at=datetime.now(UTC),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def brier_score(evaluations: list[EvaluationRow]) -> float | None:
    """Mean squared error of confidence vs realized win (lower is better)."""
    if not evaluations:
        return None
    return round(
        sum((e.confidence - (1.0 if e.win else 0.0)) ** 2 for e in evaluations)
        / len(evaluations),
        4,
    )


def calibration_buckets(
    evaluations: list[EvaluationRow], bucket_size: float = 0.1
) -> list[dict]:
    """Predicted-vs-realized points for the calibration plot (spec §7.6)."""
    buckets: dict[int, list[EvaluationRow]] = {}
    for e in evaluations:
        index = min(int(e.confidence / bucket_size), int(1 / bucket_size) - 1)
        buckets.setdefault(index, []).append(e)
    return [
        {
            "predicted": round((i + 0.5) * bucket_size, 2),
            "realized": round(sum(1 for e in members if e.win) / len(members), 4),
            "count": len(members),
        }
        for i, members in sorted(buckets.items())
    ]


def _ratio(returns: list[float], downside_only: bool) -> float | None:
    """Annualized Sharpe/Sortino over per-signal returns (rf≈0). Rough by
    design — resolved signals are irregularly spaced."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    pool = [r for r in returns if r < 0] if downside_only else returns
    if len(pool) < 2:
        return None
    ref = 0.0 if downside_only else mean
    variance = sum((r - ref) ** 2 for r in pool) / (len(pool) - 1)
    if variance == 0:
        return None
    return round(mean / math.sqrt(variance), 3)


def performance_summary(db: Session) -> dict:
    evaluations = list(db.execute(select(EvaluationRow)).scalars().all())
    if not evaluations:
        return {
            "count": 0,
            "hit_rate": None,
            "expectancy_r": None,
            "sharpe": None,
            "sortino": None,
            "brier_score": None,
            "calibration": [],
            "missed_wins": [],
        }
    wins = sum(1 for e in evaluations if e.win)
    returns = [e.return_pct for e in evaluations]
    missed_evals = sorted(
        (e for e in evaluations if e.user_decision == "skipped" and e.win),
        key=lambda e: -e.return_pct,
    )
    missed = [
        {
            "signal_id": e.signal_id,
            "ticker": e.ticker,
            "return_pct": e.return_pct,
            "r_multiple": e.r_multiple,
        }
        for e in missed_evals[:10]
    ]
    return {
        "count": len(evaluations),
        "hit_rate": round(wins / len(evaluations), 4),
        "expectancy_r": round(sum(e.r_multiple for e in evaluations) / len(evaluations), 4),
        "sharpe": _ratio(returns, downside_only=False),
        "sortino": _ratio(returns, downside_only=True),
        "brier_score": brier_score(evaluations),
        "calibration": calibration_buckets(evaluations),
        "missed_wins": missed,
    }
