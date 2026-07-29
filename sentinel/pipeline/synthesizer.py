"""Signal Synthesizer (spec §4.6).

Everything numeric is computed here in code:
  conviction  = weighted analyst agreement × regime/strategy fit
  confidence  = conviction × strategy hit-rate (neutral prior until the
                evaluation store matures) × the review's stance multiplier
  risk_score, expected return, and all price/share fields come from sizing
  and market data.

The LLM contributes exactly two things, both via the single combined review
call (agents/review.py): the plain-English explanation, and a categorical
stance whose only permitted effect is to HOLD or LOWER conviction — 'reject'
downgrades the action to NO_TRADE. It can never create a trade, raise
confidence, or alter a level. When no review is attached (Free mode, no
trigger, budget exhausted, outage) a deterministic template is used and the
signal is flagged deterministic_only.
"""

import structlog
from sqlalchemy.orm import Session

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.review import CandidateReview
from sentinel.agents.verdicts import AnalystVerdict, EvidenceItem
from sentinel.evaluation.priors import strategy_hit_rate
from sentinel.pipeline.state import CandidateState, Signal
from sentinel.strategies.base import analyst_aggregate

log = structlog.get_logger()

MAX_EVIDENCE = 12


def compute_conviction(verdicts: list[AnalystVerdict], fit_score: float) -> float:
    """Deterministic agreement × strategy fit, 0..1.

    Deliberately excludes the hit-rate prior: conviction measures "how good
    does this setup look right now", which is the right question when deciding
    whether an LLM call is worth paying for. The hit-rate belongs in the
    calibrated confidence below.
    """
    agreement = (analyst_aggregate(verdicts) + 100) / 200
    fit = max(0.0, min(100.0, fit_score)) / 100
    return round(agreement * fit, 4)


def compute_confidence(
    verdicts: list[AnalystVerdict], fit_score: float, hit_rate: float
) -> float:
    """Calibrated aggregate per spec: agreement × fit × hit-rate, all 0..1."""
    return round(compute_conviction(verdicts, fit_score) * hit_rate, 4)


def compute_risk_score(atr_pct: float | None, regime: str) -> int:
    """1 (calm, liquid) .. 10 (violent). ATR%% of price is the backbone;
    a high-volatility regime adds two points. Unknown ATR is treated as risky."""
    base = atr_pct if atr_pct is not None else 6.0
    score = round(base * 1.25)
    if regime == "high-volatility":
        score += 2
    return max(1, min(10, score))


def merge_evidence(candidate: CandidateState) -> list[EvidenceItem]:
    merged: list[EvidenceItem] = []
    if candidate.selection:
        merged.extend(
            EvidenceItem(source="strategy", datapoint=reason)
            for reason in candidate.selection.fit.reasons
        )
    for verdict in candidate.verdicts:
        merged.extend(verdict.evidence)
    if candidate.review is not None:
        merged.extend(
            EvidenceItem(source="review", datapoint=risk)
            for risk in candidate.review.key_risks
        )
    return merged[:MAX_EVIDENCE]


def _fallback_explanation(candidate: CandidateState, regime: str, action: str) -> str:
    parts = [f"{action} per {candidate.selection.fit.strategy}" if candidate.selection else action]
    parts.append(f"regime {regime}")
    if candidate.selection and candidate.selection.fit.reasons:
        parts.append("; ".join(candidate.selection.fit.reasons[:3]))
    text = f"Deterministic rationale: {' — '.join(parts)}. (LLM narrative unavailable.)"
    return text[:500]


def synthesize_signal(
    db: Session,
    candidate: CandidateState,
    regime: RegimeAssessment,
    review: CandidateReview | None = None,
) -> Signal:
    """Build the Signal for a candidate that has a strategy selection.

    The risk_check field is attached afterwards by the risk gate node — a
    Signal is never surfaced as actionable without it.
    """
    assert candidate.selection is not None, "synthesize requires a strategy selection"
    fit = candidate.selection.fit
    snap = candidate.snapshot
    action = fit.action
    sizing = candidate.sizing
    if action == "BUY" and sizing is None:
        # no valid position exists at current risk budget -> stand aside
        action = "NO_TRADE"

    review = review if review is not None else candidate.review
    # The stance can only stand the trade down, never stand it up.
    if review is not None and review.vetoes_trade and action in ("BUY", "SELL"):
        log.info("review stance downgraded action", symbol=candidate.symbol, was=action)
        action = "NO_TRADE"

    hit_rate = strategy_hit_rate(db, fit.strategy, regime=regime.regime)
    confidence = compute_confidence(candidate.verdicts, fit.score, hit_rate)
    if review is not None:
        confidence = round(confidence * review.confidence_multiplier, 4)
    atr_pct = snap.atr_pct if snap else None
    close = snap.close if snap else 0.0

    shares = None
    max_entry = stop = target = None
    expected_return = None
    if action == "BUY" and sizing is not None:
        from decimal import Decimal

        shares = sizing.shares
        max_entry = Decimal(str(sizing.max_entry_price))
        stop = Decimal(str(sizing.stop_loss))
        target = Decimal(str(sizing.take_profit))
        if close > 0:
            expected_return = round((sizing.take_profit - close) / close * 100, 4)

    has_narrative = review is not None and bool(review.explanation)
    explanation = (
        review.explanation[:500]
        if has_narrative and review is not None
        else _fallback_explanation(candidate, regime.regime, action)
    )

    return Signal(
        ticker=candidate.symbol,
        action=action,
        shares=shares,
        max_entry_price=max_entry,
        stop_loss=stop,
        take_profit=target,
        confidence=confidence,
        expected_return_pct=expected_return,
        risk_score=compute_risk_score(atr_pct, regime.regime),
        time_horizon=fit.time_horizon,
        strategy=fit.strategy,
        regime=regime.regime,
        evidence=merge_evidence(candidate),
        explanation=explanation,
        deterministic_only=not has_narrative,
    )
