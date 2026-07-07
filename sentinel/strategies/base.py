"""Strategy base (spec §4.4).

Each strategy is a class with an explicit eligible-regime list and explicit
entry/exit/stop descriptions. `evaluate` is deterministic: it maps the
candidate's computed facts to a 0–100 fit score with cited reasons. No LLM
here — the LLM's only involvement in strategy selection is the tie-break in
selector.py, and even there it picks a *name*, never a number.
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

from sentinel.agents.regime import RegimeName
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict

Action = Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
TimeHorizon = Literal["intraday", "swing_days", "position_weeks", "long_term"]


class StrategyFit(BaseModel):
    strategy: str
    action: Action
    score: float = Field(ge=0, le=100)  # deterministic fit, 0 = not applicable
    reasons: list[str] = Field(default_factory=list)
    time_horizon: TimeHorizon = "swing_days"


def analyst_aggregate(verdicts: list[AnalystVerdict]) -> float:
    """Confidence-weighted mean analyst score (-100..100); unavailable
    factors carry zero weight."""
    weighted = [(v.score * v.confidence, v.confidence) for v in verdicts if not v.unavailable]
    total_weight = sum(w for _, w in weighted)
    if total_weight <= 0:
        return 0.0
    return sum(s for s, _ in weighted) / total_weight


class Strategy(ABC):
    name: str
    eligible_regimes: frozenset[RegimeName]
    time_horizon: TimeHorizon = "swing_days"
    # Human-readable contract, surfaced in signals and the UI:
    entry_logic: str
    exit_logic: str
    stop_logic: str

    def eligible(self, regime: RegimeName) -> bool:
        return regime in self.eligible_regimes

    @abstractmethod
    def evaluate(
        self,
        snap: TechnicalSnapshot,
        screen: ScreenResult,
        verdicts: list[AnalystVerdict],
        regime: RegimeName,
    ) -> StrategyFit:
        """Deterministic fit for this candidate. Must return score 0 when the
        setup is absent — never force a trade."""
