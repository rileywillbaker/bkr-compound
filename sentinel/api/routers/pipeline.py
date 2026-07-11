"""Manual pipeline runs + last-result view. Informational only: signals are
recommendations for the user's own decision — nothing here (or anywhere)
executes a trade."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.data.discovery import DiscoveryResult, discover, get_scan_symbols
from sentinel.db.base import get_db
from sentinel.pipeline import runner
from sentinel.pipeline.state import PipelineState, Signal

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
    errors: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


def _summary(state: PipelineState) -> RunSummary:
    return RunSummary(
        run_id=str(state.run_id),
        regime=state.regime.regime if state.regime else None,
        symbols=state.symbols,
        signals=state.signals,
        errors=state.errors,
    )


@router.post("/run")
def run_pipeline(request: RunRequest, db: Session = Depends(get_db)) -> RunSummary:
    state = runner.run_scan(db, symbols=request.symbols, use_llm=request.use_llm)
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
