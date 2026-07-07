"""Signal Synthesizer (spec §4.6).

Everything numeric is computed here in code:
  confidence = weighted analyst agreement × regime/strategy fit × strategy
               hit-rate (neutral prior until the evaluation store matures)
  risk_score, expected return, and all price/share fields come from sizing
  and market data.

The LLM contributes exactly one thing: the plain-English explanation
(≤ 500 chars). When it is unavailable a deterministic template is used and
the signal is flagged deterministic_only.
"""

import json
from decimal import Decimal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.verdicts import AnalystVerdict, EvidenceItem
from sentinel.evaluation.priors import strategy_hit_rate
from sentinel.pipeline.state import CandidateState, Signal
from sentinel.providers.llm.client import LLMError, complete_json
from sentinel.strategies.base import analyst_aggregate

log = structlog.get_logger()

MAX_EVIDENCE = 12


def compute_confidence(
    verdicts: list[AnalystVerdict], fit_score: float, hit_rate: float
) -> float:
    """Calibrated aggregate per spec: agreement × fit × hit-rate, all 0..1."""
    agreement = (analyst_aggregate(verdicts) + 100) / 200
    fit = max(0.0, min(100.0, fit_score)) / 100
    return round(agreement * fit * hit_rate, 4)


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
    return merged[:MAX_EVIDENCE]


class _Explanation(BaseModel):
    text: str = Field(max_length=500)


_EXPLAIN_SYSTEM = (
    "You write the plain-English rationale for a stock signal produced by a "
    "deterministic pipeline. You are given the chosen strategy, regime, "
    "analyst verdicts, and the exact computed trade levels. Explain WHY in "
    "under 500 characters, citing concrete data points. Do not invent or "
    "alter any number. This is information, not financial advice."
)


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
    use_llm: bool = True,
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

    hit_rate = strategy_hit_rate(db, fit.strategy)
    confidence = compute_confidence(candidate.verdicts, fit.score, hit_rate)
    atr_pct = snap.atr_pct if snap else None
    close = snap.close if snap else 0.0

    shares = None
    max_entry = stop = target = None
    expected_return = None
    if action == "BUY" and sizing is not None:
        shares = sizing.shares
        max_entry = Decimal(str(sizing.max_entry_price))
        stop = Decimal(str(sizing.stop_loss))
        target = Decimal(str(sizing.take_profit))
        if close > 0:
            expected_return = round((sizing.take_profit - close) / close * 100, 4)

    deterministic_only = not use_llm
    explanation = _fallback_explanation(candidate, regime.regime, action)
    if use_llm:
        facts = {
            "symbol": candidate.symbol,
            "action": action,
            "strategy": fit.model_dump(),
            "regime": regime.model_dump(),
            "verdicts": [v.model_dump() for v in candidate.verdicts],
            "levels": sizing.model_dump() if sizing else None,
            "confidence": confidence,
        }
        try:
            result = complete_json(
                db,
                role="reasoning",
                system=_EXPLAIN_SYSTEM,
                user=json.dumps(facts, default=str),
                schema=_Explanation,
                endpoint="synthesizer.explanation",
            )
            explanation = result.text[:500]
        except LLMError as exc:
            deterministic_only = True
            log.warning("synthesizer explanation fallback", error=str(exc))

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
        deterministic_only=deterministic_only
        or all(v.deterministic_only for v in candidate.verdicts),
    )
