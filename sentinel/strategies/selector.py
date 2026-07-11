"""Strategy Selector (spec §4.4): rules first, LLM tie-break only.

Deterministic rules produce a fit score per eligible strategy. The highest
score wins. Only when the top scores are within TIE_MARGIN does the LLM get a
vote — and its vote is a strategy *name* validated against the tied set, never
a number. If the LLM is unavailable or answers outside the set, a fixed
priority order breaks the tie deterministically.
"""

import json

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict
from sentinel.providers.llm.client import LLMError, complete_json
from sentinel.strategies.base import StrategyFit
from sentinel.strategies.catalog import evaluate_all

log = structlog.get_logger()

TIE_MARGIN = 5.0  # points; closer than this is "too close to call"

# Deterministic tie order when the LLM cannot vote: prefer standing aside,
# then the less aggressive setups.
_TIE_PRIORITY = ["cash", "position-hold", "mean-reversion", "momentum-swing", "breakout"]


class SelectedStrategy(BaseModel):
    fit: StrategyFit
    considered: list[StrategyFit] = Field(default_factory=list)
    tie_break_used: bool = False
    tie_break_reason: str = ""


class _TieBreakVote(BaseModel):
    strategy: str
    reason: str = Field(max_length=300)


_TIE_SYSTEM = (
    "You are a strategy selection judge. Deterministic rules scored several "
    "trading strategies within a few points of each other for one candidate. "
    "Pick the single best-suited strategy NAME from the tied list, given the "
    "regime and analyst evidence. Respond with one of the provided names "
    "exactly; you decide nothing else."
)


def _priority_pick(fits: list[StrategyFit]) -> StrategyFit:
    return min(fits, key=lambda f: _TIE_PRIORITY.index(f.strategy))


def select_strategy(
    db: Session,
    snap: TechnicalSnapshot,
    screen: ScreenResult,
    verdicts: list[AnalystVerdict],
    regime: RegimeAssessment,
    use_llm: bool = True,
) -> SelectedStrategy:
    fits = evaluate_all(snap, screen, verdicts, regime.regime)
    ranked = sorted(fits, key=lambda f: f.score, reverse=True)
    top = ranked[0]
    tied = [f for f in ranked if top.score - f.score < TIE_MARGIN]
    if len(tied) == 1:
        return SelectedStrategy(fit=top, considered=ranked)

    winner = _priority_pick(tied)
    reason = "deterministic priority order (LLM tie-break unavailable)"
    if use_llm:
        names = [f.strategy for f in tied]
        facts = {
            "regime": regime.model_dump(),
            "tied_strategies": [f.model_dump() for f in tied],
            "analyst_verdicts": [v.model_dump() for v in verdicts],
            "candidate": {"symbol": snap.symbol, "close": snap.close},
        }
        try:
            # triage role (Haiku): the vote is a name from a fixed set, so
            # the cheap model suffices — Sonnet is reserved for synthesis
            vote = complete_json(
                db,
                role="triage",
                system=f"{_TIE_SYSTEM}\nTied names: {names}",
                user=json.dumps(facts, default=str),
                schema=_TieBreakVote,
                endpoint="strategy.tie_break",
            )
            if vote.strategy in names:
                winner = next(f for f in tied if f.strategy == vote.strategy)
                reason = vote.reason
            else:
                log.warning("tie-break vote outside tied set", vote=vote.strategy, tied=names)
        except LLMError as exc:
            log.warning("tie-break LLM unavailable", error=str(exc))
    return SelectedStrategy(
        fit=winner, considered=ranked, tie_break_used=True, tie_break_reason=reason
    )
