"""Typed pipeline state + the Signal schema (spec §4).

Numeric trade parameters (shares, prices) enter a Signal from exactly one
place: sentinel.portfolio.sizing.size_position plus market data. LLM output
only ever fills prose fields (explanation, evidence citations, tie-break
names) — never numbers.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict, EvidenceItem
from sentinel.data.context import MarketContext
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
    evidence: list[EvidenceItem] = Field(default_factory=list)
    explanation: str = Field(max_length=500)
    risk_check: RiskCheckResult | None = None  # None only for NO_TRADE/HOLD
    alert_sent: bool = False
    user_decision: Literal["taken", "skipped", "modified", "pending"] | None = None
    deterministic_only: bool = False  # True when the LLM was skipped/unavailable

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


class PipelineState(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    symbols: list[str] = Field(default_factory=list)
    use_llm: bool = True
    context: MarketContext | None = None
    regime: RegimeAssessment | None = None
    candidates: dict[str, CandidateState] = Field(default_factory=dict)
    signals: list[Signal] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
