"""Strategy hit-rate priors for confidence calibration (spec §4.6).

Phase 6's evaluation loop replaces this with real per-strategy hit-rates from
resolved signals. Until 30+ resolved signals exist for a strategy, the spec
mandates a neutral prior — which is all this returns for now.
"""

from sqlalchemy.orm import Session

NEUTRAL_HIT_RATE = 0.5
MIN_RESOLVED_FOR_REAL_RATE = 30


def strategy_hit_rate(db: Session, strategy: str) -> float:
    """Neutral prior until the Phase 6 evaluation store lands."""
    return NEUTRAL_HIT_RATE
