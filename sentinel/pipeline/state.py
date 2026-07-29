"""Typed pipeline state + the Signal schema (spec §4).

Numeric trade parameters (shares, prices) enter a Signal from exactly one
place: sentinel.portfolio.sizing.size_position plus market data. LLM output
only ever fills prose fields (explanation, evidence citations, tie-break
names, an advisory stance) — never numbers.

The state also carries the run's COST accounting: which operating mode and
depth applied, how many candidates survived each deterministic stage, and how
many LLM calls were actually made. That funnel is what makes "we analysed 700
names for $0.00" auditable rather than a claim.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.review import CandidateReview
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict, EvidenceItem
from sentinel.data.context import MarketContext
from sentinel.modes import DEFAULT_MODE, Depth, OperatingMode
from sentinel.portfolio.manager import PositionReview
from sentinel.portfolio.sizing import SizingResult
from sentinel.risk.engine import RiskCheckResult
from sentinel.strategies.base import Action, TimeHorizon
from sentinel.strategies.selector import SelectedStrategy


class Signal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ticker: str
    action: Action
    shares: int | None = None  # exact count; required for BUY/SELL
    max_entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    confidence: float = Field(ge=0, le=1)
    expected_return_pct: float | None = None
    risk_score: int = Field(ge=1, le=10)
    time_horizon: TimeHorizon
    strategy: str
    regime: str
    book: str = "core"  # "core" (long-term pipeline) or "swing" (swing pipeline)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    explanation: str = Field(max_length=500)
    risk_check: RiskCheckResult | None = None  # None only for NO_TRADE/HOLD
    alert_sent: bool = False
    user_decision: Literal["taken", "skipped", "modified", "pending"] | None = None
    # True when no LLM interpretation is attached (Free mode, no trigger fired,
    # budget exhausted, or an outage). The signal is still complete and safe.
    deterministic_only: bool = False

    @property
    def actionable(self) -> bool:
        return (
            self.action in ("BUY", "SELL")
            and self.risk_check is not None
            and self.risk_check.approved
        )


class CandidateState(BaseModel):
    symbol: str
    snapshot: TechnicalSnapshot | None = None
    screen: ScreenResult | None = None
    verdicts: list[AnalystVerdict] = Field(default_factory=list)
    selection: SelectedStrategy | None = None
    sizing: SizingResult | None = None
    # Deterministic agreement × strategy-fit, 0..1. Distinct from the final
    # calibrated confidence (which also folds in the strategy's hit-rate prior)
    # and used purely to decide whether an LLM call is worth its cost.
    conviction: float = 0.0
    # Provisional risk evaluation run BEFORE any LLM spend, so the model only
    # ever sees candidates the risk engine would approve. The authoritative
    # check is still the terminal risk gate.
    risk_pre: RiskCheckResult | None = None
    review: CandidateReview | None = None
    llm_trigger: str = ""


class PipelineState(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    symbols: list[str] = Field(default_factory=list)
    use_llm: bool = True  # False forces depth="none" regardless of mode
    mode: OperatingMode = DEFAULT_MODE
    depth: Depth = "none"
    on_demand: bool = False  # user-initiated run (chat, "analyze X", research)
    context: MarketContext | None = None
    regime: RegimeAssessment | None = None
    candidates: dict[str, CandidateState] = Field(default_factory=dict)
    position_reviews: list[PositionReview] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    # Deterministic funnel: how many symbols survived each stage.
    funnel: dict[str, int] = Field(default_factory=dict)
    llm_calls: int = 0
    errors: list[str] = Field(default_factory=list)
