"""Final LLM review — the ONE call B-Quant is willing to pay for.

Claude is the last step in the pipeline, never the first. By the time a
candidate reaches this module it has already survived, in deterministic Python:

    universe → technical filters → liquidity filters → fundamental/quality
    filters → analyst fact table → strategy fit → position sizing → risk engine

Everything numeric is settled. What remains is judgment that code genuinely
cannot supply: does the *narrative* around this name contradict the setup, and
how would you explain the trade to a human in plain English.

Cost design
-----------
The old pipeline spent SEVEN calls per candidate (five analysts + a strategy
tie-break + a synthesis narrative) on every screened name. This spends ONE call
per finalist, and only for finalists — typically 1–3 per trading day.

Everything the call sees is precomputed, so the request is small and the
response is short. Results are cached by a fingerprint of the material facts
(sentinel/data/cache.py): the identical situation is never paid for twice.

Safety
------
The model returns a categorical STANCE from a fixed set, never a number. Code
maps the stance to a fixed multiplier, and the mapping is one-directional:

    confirm →  keep the deterministic confidence
    caution →  REDUCE it
    reject  →  downgrade the action to NO_TRADE

A review can only make the system more conservative. It can never create a
trade, raise confidence, resize a position, move a stop, or overturn a risk
veto — the risk gate runs again after this stage and remains absolute.
"""

import json
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict
from sentinel.data.cache import TTL_LLM_REVIEW, cache_get, cache_key, cache_set, fingerprint
from sentinel.data.context import SymbolContext
from sentinel.portfolio.sizing import SizingResult
from sentinel.providers.llm.client import LLMError, complete_json
from sentinel.risk.engine import RiskCheckResult

log = structlog.get_logger()

Stance = Literal["confirm", "caution", "reject"]

# Fixed, code-owned mapping. The model picks a NAME from the enum above; the
# numbers live here and only ever move confidence downward.
STANCE_CONFIDENCE_MULTIPLIER: dict[str, float] = {
    "confirm": 1.0,
    "caution": 0.85,
    "reject": 0.0,
}

_REVIEW_SYSTEM = (
    "You are the final reviewer on a disciplined, deterministic trading "
    "research system. A candidate has ALREADY passed every quantitative "
    "filter: technicals, liquidity, fundamentals, strategy fit, position "
    "sizing, and a hard risk engine. All numbers you are shown were computed "
    "in code and are final — never restate them incorrectly, never invent "
    "new ones, and never suggest different share counts, entries, stops or "
    "targets.\n\n"
    "Your job is exactly two things:\n"
    "1. stance — 'confirm' if the qualitative picture is consistent with the "
    "quantitative setup; 'caution' if there is a real but non-disqualifying "
    "concern (thin catalyst, conflicting news, crowded trade, macro headwind); "
    "'reject' if something in the news, filings, or fundamentals materially "
    "contradicts the setup (fraud allegations, guidance cut, going-concern "
    "doubt, pending acquisition that caps upside, imminent binary event). "
    "Standing aside is always an acceptable answer — you are not here to "
    "justify a trade.\n"
    "2. explanation — under 500 characters, plain English, citing the "
    "specific data points that mattered. Write for a non-professional "
    "investor. This is information, not financial advice.\n\n"
    "Also list up to three concrete key_risks. Be terse and concrete."
)


class LLMReviewPayload(BaseModel):
    """Schema the model must satisfy. Categories and prose only — no numbers."""

    stance: Stance
    explanation: str = Field(max_length=500)
    key_risks: list[str] = Field(default_factory=list)


class CandidateReview(BaseModel):
    """Result of the review stage for one candidate."""

    symbol: str
    stance: Stance = "confirm"
    explanation: str = ""
    key_risks: list[str] = Field(default_factory=list)
    trigger: str = ""  # why this candidate was worth spending on
    llm_used: bool = False  # a live call was made (False when served from cache)
    from_cache: bool = False
    fact_hash: str = ""

    @property
    def confidence_multiplier(self) -> float:
        return STANCE_CONFIDENCE_MULTIPLIER.get(self.stance, 1.0)

    @property
    def vetoes_trade(self) -> bool:
        return self.stance == "reject"


def _round(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def build_fact_pack(
    symbol: str,
    snap: TechnicalSnapshot,
    screen: ScreenResult,
    sym_ctx: SymbolContext | None,
    verdicts: list[AnalystVerdict],
    regime: RegimeAssessment,
    strategy: str,
    action: str,
    sizing: SizingResult | None,
    risk_check: RiskCheckResult | None,
    base_confidence: float,
    macro: dict | None = None,
) -> dict:
    """The complete deterministic evidence bundle handed to the reviewer.

    Deliberately compact: rounded indicators, the headlines themselves, and
    the already-computed decision. Small prompts are cheap prompts, and every
    field here came out of Python.
    """
    news = []
    if sym_ctx is not None:
        news = [
            {"headline": n.headline, "source": n.source, "published_at": str(n.published_at)}
            for n in sym_ctx.news[:12]
        ]
    return {
        "symbol": symbol,
        "proposed_action": action,
        "strategy": strategy,
        "regime": {"regime": regime.regime, "detail": regime.detail},
        "deterministic_confidence": round(base_confidence, 4),
        "technicals": {
            "close": _round(snap.close),
            "rsi14": _round(snap.rsi14, 1),
            "macd_hist": _round(snap.macd_hist, 3),
            "atr_pct": _round(snap.atr_pct, 2),
            "adx14": _round(snap.adx14, 1),
            "above_sma20": snap.above_sma20,
            "above_sma50": snap.above_sma50,
            "above_sma200": snap.above_sma200,
            "relative_volume": _round(snap.relative_volume, 2),
            "pct_from_52w_high": _round(snap.pct_from_52w_high, 1),
            "pct_from_52w_low": _round(snap.pct_from_52w_low, 1),
            "avg_dollar_volume20": _round(snap.avg_dollar_volume20, 0),
        },
        "screen_scores": {
            "momentum": screen.momentum_score,
            "trend": screen.trend_score,
            "volume": screen.volume_score,
        },
        "fundamentals": {
            "sector": sym_ctx.sector if sym_ctx else "",
            "market_cap_millions": _round(sym_ctx.market_cap, 0) if sym_ctx else None,
            "pe_ttm": _round(sym_ctx.pe) if sym_ctx else None,
            "ps_ttm": _round(sym_ctx.ps) if sym_ctx else None,
            "beta": _round(sym_ctx.beta) if sym_ctx else None,
            "next_earnings": (
                str(sym_ctx.next_earnings.date)
                if sym_ctx and sym_ctx.next_earnings
                else None
            ),
        },
        "recent_headlines": news,
        "macro": macro or {},
        "deterministic_analyst_reads": [
            {
                "analyst": v.analyst,
                "score": v.score,
                "confidence": v.confidence,
                "summary": v.summary,
                "unavailable": v.unavailable,
            }
            for v in verdicts
        ],
        "computed_levels": sizing.model_dump() if sizing else None,
        "risk_engine": {
            "approved": risk_check.approved if risk_check else None,
            "failed_rules": risk_check.failed_rules() if risk_check else [],
        },
    }


def facts_fingerprint(facts: dict) -> str:
    """Hash of the facts that would change a reviewer's mind.

    Deliberately excludes noise (exact prices to the cent, headline
    timestamps, macro tails) so a quiet day doesn't invalidate the cache and
    re-buy an identical opinion. Includes the headline TEXT, so genuinely new
    news always forces a fresh review.
    """
    tech = facts.get("technicals", {})
    material = {
        "symbol": facts.get("symbol"),
        "action": facts.get("proposed_action"),
        "strategy": facts.get("strategy"),
        "regime": (facts.get("regime") or {}).get("regime"),
        # bucketed: only a meaningful move in an indicator counts as change
        "rsi_bucket": None if tech.get("rsi14") is None else round(tech["rsi14"] / 5),
        "trend": [tech.get("above_sma20"), tech.get("above_sma50"), tech.get("above_sma200")],
        "macd_sign": None if tech.get("macd_hist") is None else tech["macd_hist"] > 0,
        "off_high_bucket": (
            None
            if tech.get("pct_from_52w_high") is None
            else round(tech["pct_from_52w_high"] / 5)
        ),
        "rvol_bucket": (
            None if tech.get("relative_volume") is None else round(tech["relative_volume"], 1)
        ),
        "headlines": sorted(h.get("headline", "") for h in facts.get("recent_headlines", [])),
        "next_earnings": (facts.get("fundamentals") or {}).get("next_earnings"),
        "risk_approved": (facts.get("risk_engine") or {}).get("approved"),
    }
    return fingerprint(material)


def _deterministic_review(symbol: str, trigger: str, fact_hash: str = "") -> CandidateReview:
    """Neutral stance used when the model is unavailable (budget, keys, outage).

    Neutral means *no adjustment*: the deterministic pipeline's own verdict
    stands unchanged. Degrading never invents optimism.
    """
    return CandidateReview(
        symbol=symbol,
        stance="confirm",
        explanation="",
        trigger=trigger,
        llm_used=False,
        fact_hash=fact_hash,
    )


def review_candidate(
    db: Session,
    symbol: str,
    facts: dict,
    trigger: str = "",
    role: str = "review",
    use_cache: bool = True,
) -> CandidateReview:
    """One combined review call (or a free cache hit).

    Never raises: an unavailable model degrades to the neutral review above and
    the signal is flagged deterministic_only downstream.
    """
    fact_hash = facts_fingerprint(facts)
    key = cache_key("llm_review", symbol, fact_hash)

    if use_cache:
        cached = cache_get(db, key)
        if isinstance(cached, dict):
            try:
                review = CandidateReview.model_validate(cached)
            except ValueError:
                review = None  # corrupt entry: fall through and re-analyze
            if review is not None:
                log.info("llm review cache hit", symbol=symbol, fact_hash=fact_hash)
                return review.model_copy(
                    update={"from_cache": True, "llm_used": False, "trigger": trigger}
                )

    try:
        payload = complete_json(
            db,
            role=role,
            system=_REVIEW_SYSTEM,
            user=json.dumps(facts, default=str),
            schema=LLMReviewPayload,
            endpoint="review.candidate",
        )
    except LLMError as exc:
        log.warning("llm review unavailable — deterministic verdict stands",
                    symbol=symbol, error=str(exc))
        return _deterministic_review(symbol, trigger, fact_hash)

    review = CandidateReview(
        symbol=symbol,
        stance=payload.stance,
        explanation=payload.explanation[:500],
        key_risks=[r[:160] for r in payload.key_risks[:3]],
        trigger=trigger,
        llm_used=True,
        fact_hash=fact_hash,
    )
    cache_set(db, key, review.model_dump(), TTL_LLM_REVIEW, kind="llm_review")
    return review
