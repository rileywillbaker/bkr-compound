"""Manual pipeline runs + last-result view. Informational only: signals are
recommendations for the user's own decision — nothing here (or anywhere)
executes a trade."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.data.discovery import DiscoveryResult, discover, get_scan_symbols
from sentinel.db.base import get_db
from sentinel.modes import get_policy
from sentinel.pipeline import runner
from sentinel.pipeline.state import PipelineState, Signal
from sentinel.portfolio.manager import PositionReview

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class RunRequest(BaseModel):
    # default: today's scan set (discovery candidates + watchlist + positions);
    # any ticker may be passed explicitly — manual runs never send alerts
    symbols: list[str] | None = None
    use_llm: bool = True


class RunSummary(BaseModel):
    run_id: str
    regime: str | None
    symbols: list[str]
    signals: list[Signal]
    position_reviews: list[PositionReview] = Field(default_factory=list)
    # Cost accounting: which mode/depth applied, how many symbols survived
    # each deterministic stage, and how many LLM calls were actually made.
    mode: str = ""
    depth: str = ""
    funnel: dict[str, int] = Field(default_factory=dict)
    llm_calls: int = 0
    errors: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


def _summary(state: PipelineState) -> RunSummary:
    return RunSummary(
        run_id=str(state.run_id),
        regime=state.regime.regime if state.regime else None,
        symbols=state.symbols,
        signals=state.signals,
        position_reviews=state.position_reviews,
        mode=state.mode,
        depth=state.depth,
        funnel=state.funnel,
        llm_calls=state.llm_calls,
        errors=state.errors,
    )


@router.post("/run")
def run_pipeline(request: RunRequest, db: Session = Depends(get_db)) -> RunSummary:
    state = runner.run_scan(db, symbols=request.symbols, use_llm=request.use_llm)
    db.commit()
    return _summary(state)


class ResearchRequest(BaseModel):
    """User-initiated deep analysis of one ticker ("Should I buy NVDA?")."""

    symbol: str = Field(min_length=1, max_length=12)


@router.post("/research")
def research(request: ResearchRequest, db: Session = Depends(get_db)) -> RunSummary:
    """Full multi-agent analysis of a single ticker, on request.

    This is the one path that deliberately spends: it runs the complete
    analyst fan-out rather than the single combined review. It works for ANY
    ticker, backfilling data on demand, and still terminates in the risk gate.
    Blocked in Free mode, where the answer is "switch modes first" rather than
    a silent charge.
    """
    from sentinel.data.ingest import ensure_symbol_data

    policy = get_policy(db)
    if policy.on_demand_depth == "none":
        raise HTTPException(
            409,
            "Free mode makes no AI calls. Switch to Smart mode in Settings to run "
            "a research analysis, or use /api/pipeline/run for the deterministic read.",
        )
    symbol = request.symbol.strip().upper()
    ensure_symbol_data(db, symbol)
    state = runner.run_scan(db, symbols=[symbol], on_demand=True)
    db.commit()
    return _summary(state)


@router.get("/last")
def last_result() -> RunSummary | None:
    state = runner.last_run()
    return _summary(state) if state else None


@router.get("/scan-symbols")
def scan_symbols(db: Session = Depends(get_db)) -> dict:
    """What the next scheduled scan will analyze (candidates + watchlist +
    positions)."""
    return {"symbols": get_scan_symbols(db)}


@router.post("/discover")
def run_discovery(db: Session = Depends(get_db)) -> DiscoveryResult:
    """Manual discovery sweep over the full universe (deterministic, no LLM)."""
    result = discover(db)
    db.commit()
    return result
