"""Operating modes: the cost policy that decides IF and HOW DEEPLY an LLM runs.

B-Quant treats tokens as a scarce resource. Deterministic computation is
unlimited and free; every LLM call must earn its place. The mode is the single
switch that governs automatic spend:

  free     — no LLM, ever. Unlimited deterministic scans, technicals, risk
             engine, sizing, discovery, portfolio management, alerts, briefs.
             Target cost: $0/month.
  smart    — DEFAULT. Scheduled scans may spend on a *single* combined review
             call per finalist, and only for candidates that already cleared
             every deterministic filter AND the risk engine. Target: 1–5 LLM
             analyses per trading day. User-initiated research still gets the
             full multi-agent treatment.
  research — everything smart does, plus scheduled scans also use full
             multi-agent depth. Explicitly the expensive mode.

"Depth" is the second axis:
  none   — deterministic verdicts only; zero calls.
  review — ONE structured call per finalist that folds the five analyst
           interpretations, the strategy sanity check, and the plain-English
           explanation into a single request (see agents/review.py). Replaces
           the old 7-calls-per-candidate fan-out.
  full   — the original per-analyst fan-out plus the selector tie-break and a
           dedicated synthesis call. Reserved for user-initiated research.

None of this can weaken the risk engine: mode affects only whether prose and
an advisory stance are attached to a signal. Shares, prices, stops, targets,
and the risk veto are deterministic in every mode.
"""

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

OperatingMode = Literal["free", "smart", "research"]
Depth = Literal["none", "review", "full"]

MODE_KEY = "operating_mode"
DEFAULT_MODE: OperatingMode = "smart"

VALID_MODES: tuple[OperatingMode, ...] = ("free", "smart", "research")


class LLMPolicy(BaseModel):
    """Resolved, immutable cost policy for one operating mode."""

    mode: OperatingMode
    scan_depth: Depth  # scheduled/automatic runs
    on_demand_depth: Depth  # user-initiated runs (chat, "analyze X", research)
    max_llm_candidates_per_scan: int = Field(ge=0)
    min_conviction_for_llm: float = Field(ge=0, le=1)
    label: str
    description: str

    @property
    def allows_any_llm(self) -> bool:
        return self.scan_depth != "none" or self.on_demand_depth != "none"

    def depth_for(self, on_demand: bool) -> Depth:
        return self.on_demand_depth if on_demand else self.scan_depth


_POLICIES: dict[OperatingMode, LLMPolicy] = {
    "free": LLMPolicy(
        mode="free",
        scan_depth="none",
        on_demand_depth="none",
        max_llm_candidates_per_scan=0,
        min_conviction_for_llm=1.0,
        label="Free",
        description=(
            "No AI calls at all. Deterministic screening, technicals, risk engine, "
            "position sizing, discovery, portfolio review, alerts and daily briefs "
            "all run normally. Recurring cost: $0."
        ),
    ),
    "smart": LLMPolicy(
        mode="smart",
        scan_depth="review",
        on_demand_depth="full",
        max_llm_candidates_per_scan=3,
        min_conviction_for_llm=0.40,
        label="Smart (recommended)",
        description=(
            "AI runs only on finalists that already passed every deterministic "
            "filter and the risk engine — at most 3 per scan, one call each, and "
            "never twice on an unchanged situation. Asking a question yourself "
            "still triggers the full multi-agent analysis."
        ),
    ),
    "research": LLMPolicy(
        mode="research",
        scan_depth="full",
        on_demand_depth="full",
        max_llm_candidates_per_scan=5,
        min_conviction_for_llm=0.30,
        label="Research (highest cost)",
        description=(
            "Scheduled scans also use the full multi-agent analysis on finalists. "
            "Costs several times more than Smart for a modest quality gain — use "
            "it deliberately, not as a default."
        ),
    ),
}


def policy_for(mode: str | None) -> LLMPolicy:
    """Policy for a mode name; unknown/empty falls back to the default mode."""
    name = str(mode or "").lower()
    if name not in VALID_MODES:
        return _POLICIES[DEFAULT_MODE]
    return _POLICIES[name]  # type: ignore[index]


def all_policies() -> list[LLMPolicy]:
    return [_POLICIES[m] for m in VALID_MODES]


def get_mode(db: Session) -> OperatingMode:
    from sentinel.db.settings_store import get_setting

    stored = str(get_setting(db, MODE_KEY, DEFAULT_MODE) or "").lower()
    return stored if stored in VALID_MODES else DEFAULT_MODE  # type: ignore[return-value]


def set_mode(db: Session, mode: str) -> OperatingMode:
    from sentinel.db.settings_store import set_setting

    cleaned = str(mode or "").lower()
    if cleaned not in VALID_MODES:
        raise ValueError(f"unknown operating mode '{mode}' (expected one of {VALID_MODES})")
    set_setting(db, MODE_KEY, cleaned)
    return cleaned  # type: ignore[return-value]


def get_policy(db: Session) -> LLMPolicy:
    return policy_for(get_mode(db))
