"""Swing-suitability screener (deterministic, no LLM).

Answers "which stocks are worth swing-trading right now?" over the S&P 500
universe. It only ever ADDS filters on top of the risk profile — it never
relaxes a safety limit. A name must be:

  - liquid enough to actually fill stops/targets (ADV floor well above the
    long-term $5M floor),
  - moving enough to be worth a swing (ATR floor) yet still inside the risk
    profile's ATR ceiling,
  - long enough in history and structurally clean (a definable support level
    or pressing the 52-week high).

Eligible names are ranked by a deterministic pre-score and capped, so only a
focused set proceeds to the (LLM) analysts — bounding both noise and cost.
"""

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.screener import ScreenerParams, ScreenResult, screen
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.data.context import MarketContext
from sentinel.data.universe import held_symbols, load_static_universe
from sentinel.db.settings_store import get_watchlist


class SwingScreenParams(BaseModel):
    """Swing-suitability filters (stricter than the long-term screener)."""

    min_price: float = Field(default=5.0, ge=0)
    min_avg_dollar_volume: float = Field(default=20_000_000, ge=0)  # liquidity floor
    min_atr_pct: float = Field(default=2.0, ge=0)  # movement floor
    max_atr_pct: float = Field(default=8.0, gt=0)  # ceiling (mirrors risk cap)
    # Same quality bar as the long-term book: no micro caps, no names whose
    # data coverage is too thin to judge. Stated explicitly here rather than
    # inherited by accident from the base screener's defaults.
    min_market_cap_millions: float | None = 2_000.0
    require_sector: bool = True
    max_candidates: int = Field(default=12, gt=0)  # cap that proceeds to analysts


class SwingCandidate(BaseModel):
    symbol: str
    screen: ScreenResult  # `.eligible` reflects SWING eligibility; carries factor scores
    prescore: float


def swing_universe(db: Session) -> list[str]:
    """The S&P 500 static universe + highlighted watchlist + held positions.

    Held names are always included so open swing positions keep being
    monitored even if they churn out of the index list.
    """
    symbols = set(load_static_universe()) | set(get_watchlist(db)) | set(held_symbols(db))
    return sorted(symbols)


def _prescore(snap: TechnicalSnapshot, base: ScreenResult) -> float:
    """Deterministic setup-readiness score used only to rank for the cap."""
    score = base.trend_score + max(0.0, base.momentum_score) * 0.5 + base.volume_score * 0.5
    if snap.pct_from_52w_high is not None and snap.pct_from_52w_high >= -5:
        score += 20  # pressing the highs (breakout-ready)
    if snap.rsi14 is not None and snap.rsi14 <= 35:
        score += 20  # washed out (bounce-ready)
    return round(score, 2)


def _has_clean_structure(snap: TechnicalSnapshot) -> bool:
    """A definable reference level: swing support below, or pressing the highs."""
    near_high = snap.pct_from_52w_high is not None and snap.pct_from_52w_high >= -3
    return snap.support is not None or near_high


def swing_screen(
    context: MarketContext,
    technicals: dict[str, TechnicalSnapshot],
    params: SwingScreenParams | None = None,
) -> tuple[list[SwingCandidate], int]:
    """Return (eligible candidates capped+ranked, number of symbols scanned)."""
    params = params or SwingScreenParams()
    base_params = ScreenerParams(
        min_price=params.min_price,
        min_avg_dollar_volume=params.min_avg_dollar_volume,
        max_atr_pct=params.max_atr_pct,
        min_market_cap_millions=params.min_market_cap_millions,
        require_sector=params.require_sector,
    )
    base_results = {r.symbol: r for r in screen(context, technicals, base_params)}

    candidates: list[SwingCandidate] = []
    for symbol, base in base_results.items():
        snap = technicals.get(symbol)
        reasons = list(base.reasons)
        eligible = base.eligible
        if eligible and snap is not None:
            if snap.atr_pct is None or snap.atr_pct < params.min_atr_pct:
                eligible = False
                reasons.append("too quiet for a swing (ATR below movement floor)")
            elif not _has_clean_structure(snap):
                eligible = False
                reasons.append("no clean structural level (support / 52-week high)")
        swing_result = base.model_copy(update={"eligible": eligible, "reasons": reasons})
        prescore = _prescore(snap, base) if snap is not None else 0.0
        candidates.append(
            SwingCandidate(symbol=symbol, screen=swing_result, prescore=prescore)
        )

    passing = [c for c in candidates if c.screen.eligible]
    passing.sort(key=lambda c: c.prescore, reverse=True)
    return passing[: params.max_candidates], len(technicals)
