"""Strategy hit-rate priors for confidence calibration (spec §4.6, §9).

Reads the nightly-recomputed strategy_stats rollup. Until a strategy has
MIN_RESOLVED_FOR_REAL_RATE resolved signals, the spec mandates a neutral
prior. The real rate is blended toward neutral by sample size so a lucky
early streak can't inflate confidence.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import StrategyStatRow

NEUTRAL_HIT_RATE = 0.5
MIN_RESOLVED_FOR_REAL_RATE = 30
BLEND_WEIGHT = 50  # pseudo-count of neutral observations in the blend


def strategy_hit_rate(db: Session, strategy: str, regime: str | None = None) -> float:
    """Calibrated hit-rate prior for a strategy (optionally regime-specific).

    Falls back: (strategy, regime) → (strategy, "*") → neutral. Rows with
    fewer than MIN_RESOLVED_FOR_REAL_RATE resolutions stay neutral.
    """
    for scope in ([regime] if regime else []) + ["*"]:
        row = db.execute(
            select(StrategyStatRow).where(
                StrategyStatRow.strategy == strategy, StrategyStatRow.regime == scope
            )
        ).scalars().first()
        if row is not None and row.resolved_count >= MIN_RESOLVED_FOR_REAL_RATE:
            blended = (
                row.hit_rate * row.resolved_count + NEUTRAL_HIT_RATE * BLEND_WEIGHT
            ) / (row.resolved_count + BLEND_WEIGHT)
            return round(blended, 4)
    return NEUTRAL_HIT_RATE
