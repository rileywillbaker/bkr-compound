"""The trend agent's one optional paid call — per THEME, never per ticker.

Everything a user sees in the trend report is produced without this module:
the strength score, the component breakdown, the explanation, the stock
ranking and every dollar amount. Free mode omits this call entirely and loses
nothing but a second opinion on legitimacy.

What it adds, and why code can't
--------------------------------
The deterministic hype guard in `scoring.py` catches the *statistical*
signature of a hype cycle — attention without confirmation, a basket carried
by one name. What it cannot catch is a narrative that is structurally
mispriced for reasons only present in the language: a "nuclear renaissance"
story where every headline traces back to one press release; a theme whose
constituents are exposed to the wrong end of it (a "clean energy" name that
actually sells to the incumbent); a policy that reads bullish and is in fact a
subsidy phase-out. That is a reading task, and it is the one thing here worth
paying for.

Cost design
-----------
* One call per theme, and only for the small number of themes that reach the
  report — never per candidate, never over the corpus.
* Hard cap per run from the operating mode (`modes.py`): 0 in Free.
* Cached on a fingerprint of the material evidence, so an unchanged story is
  never re-bought. The fingerprint deliberately buckets the score and excludes
  headline ordering, so ordinary day-to-day drift is a cache hit.

Safety
------
The model returns a categorical VERDICT from a fixed set, plus prose. Code owns
the mapping, and it is one-directional:

    confirms   → keep the deterministic score
    overstated → REDUCE it
    hype       → reduce it further AND force the legitimacy label to "hype"

A trend review can never raise a score, promote a legitimacy label, add a
stock, change a rank, or alter a dollar amount. Those all come from
deterministic code that this module never touches.
"""

from __future__ import annotations

import json
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.data.cache import TTL_LLM_REVIEW, cache_get, cache_key, cache_set, fingerprint
from sentinel.providers.llm.client import LLMError, complete_json
from sentinel.trends.scoring import TrendScore

log = structlog.get_logger()

TrendVerdict = Literal["confirms", "overstated", "hype"]

# Code-owned, one-directional. The model picks a NAME; the numbers live here.
VERDICT_SCORE_MULTIPLIER: dict[str, float] = {
    "confirms": 1.0,
    "overstated": 0.85,
    "hype": 0.6,
}

_SYSTEM = (
    "You are the sceptic on a disciplined investment research system. You are "
    "shown a market theme that a deterministic engine has already scored from "
    "free public evidence: news volume and tone, government and regulatory "
    "activity, how the constituent basket performed against the market, "
    "thematic ETF behaviour, and social discussion. Every number you are shown "
    "was computed in code and is final. Never restate a number incorrectly, "
    "never invent one, and never suggest a different score, ranking, or "
    "position size.\n\n"
    "Answer exactly three things:\n"
    "1. verdict — 'confirms' if the evidence genuinely supports a durable, "
    "investable trend; 'overstated' if something real is happening but the "
    "coverage is running ahead of it; 'hype' if this looks like a narrative "
    "cycle rather than a durable change (recycled press releases, a story with "
    "no economics behind it, constituents that don't actually benefit, or "
    "policy that reads bullish but isn't).\n"
    "2. assessment — under 600 characters, plain English, for a "
    "non-professional investor. Say what would have to be true for this trend "
    "to persist, and cite the specific evidence that mattered.\n"
    "3. key_risks — up to three concrete, specific risks to this theme.\n\n"
    "Being unimpressed is always an acceptable answer. You are not here to "
    "justify a theme. This is information, not financial advice."
)


class TrendReviewPayload(BaseModel):
    """Schema the model must satisfy. A category and prose — no numbers.

    The instruction asks for under 600 characters but the schema tolerates 800:
    a validation failure costs a full retry (observed once on the first live
    run), which is real money for a limit that exists only to keep the report
    readable. The text is truncated to 600 on the way out regardless.
    """

    verdict: TrendVerdict
    assessment: str = Field(max_length=800)
    key_risks: list[str] = Field(default_factory=list)


class TrendReview(BaseModel):
    theme: str
    verdict: TrendVerdict = "confirms"
    assessment: str = ""
    key_risks: list[str] = Field(default_factory=list)
    llm_used: bool = False
    from_cache: bool = False
    fact_hash: str = ""

    @property
    def score_multiplier(self) -> float:
        return VERDICT_SCORE_MULTIPLIER.get(self.verdict, 1.0)

    @property
    def forces_hype_label(self) -> bool:
        return self.verdict == "hype"


def build_fact_pack(score: TrendScore) -> dict:
    """The deterministic evidence bundle handed to the reviewer.

    Compact by design — small prompts are cheap prompts, and every field here
    came out of Python.
    """
    read = score.market_read
    basket = read.basket if read else None
    return {
        "theme": score.theme,
        "theme_name": score.name,
        "deterministic_score": score.score,
        "deterministic_legitimacy": score.legitimacy,
        "hype_flags_already_detected": score.hype_flags,
        "components": {
            c.name: {"score": c.score, "detail": c.detail, "measured": c.covered}
            for c in score.components
        },
        "basket": (
            {
                "members_with_data": basket.symbols_with_data,
                "return_21d_pct": basket.return_21d_pct,
                "excess_vs_market_21d_pct": basket.excess_21d_pct,
                "median_return_21d_pct": basket.median_return_21d_pct,
                "breadth_pct": basket.breadth_pct,
                "top_name_share_of_gain_pct": basket.concentration_pct,
            }
            if basket
            else None
        ),
        "etfs_accumulating": (read.etfs_accumulating if read else []),
        "recent_headlines": score.evidence.get("headlines", [])[:6],
        "government_activity": score.evidence.get("government", [])[:5],
        "etf_holdings_increases": [
            {"symbol": a.get("symbol"), "etf": a.get("etf"), "weight_change": a.get("weight_change")}
            for a in score.evidence.get("etf_accumulation", [])[:8]
        ],
        "social": score.evidence.get("social", {}),
        "candidate_symbols": score.symbols[:15],
        "sources_unavailable_today": score.evidence.get("coverage_gaps", []),
    }


def facts_fingerprint(facts: dict) -> str:
    """Hash of what would change a reviewer's mind.

    The score is bucketed to 5 points and headlines are sorted, so ordinary
    drift and reordering are cache hits while genuinely new coverage or a
    material score move forces a fresh read.
    """
    basket = facts.get("basket") or {}
    material = {
        "theme": facts.get("theme"),
        "score_bucket": round(float(facts.get("deterministic_score") or 0) / 5),
        "legitimacy": facts.get("deterministic_legitimacy"),
        "flags": sorted(facts.get("hype_flags_already_detected") or []),
        "headlines": sorted(facts.get("recent_headlines") or []),
        "government": sorted(facts.get("government_activity") or []),
        "etfs_accumulating": sorted(facts.get("etfs_accumulating") or []),
        "excess_bucket": (
            None
            if basket.get("excess_vs_market_21d_pct") is None
            else round(float(basket["excess_vs_market_21d_pct"]) / 3)
        ),
        "symbols": sorted(facts.get("candidate_symbols") or []),
    }
    return fingerprint(material)


def _neutral(theme: str, fact_hash: str = "") -> TrendReview:
    """Used when no model is available (Free mode, budget, outage, no key).

    Neutral means NO ADJUSTMENT — the deterministic assessment stands exactly
    as computed. Degrading never invents optimism, and never invents doubt.
    """
    return TrendReview(theme=theme, verdict="confirms", llm_used=False, fact_hash=fact_hash)


def review_trend(
    db: Session, score: TrendScore, role: str = "trend_review", use_cache: bool = True
) -> TrendReview:
    """One combined review call for one theme (or a free cache hit).

    Never raises: an unavailable model degrades to the neutral review above.
    """
    facts = build_fact_pack(score)
    fact_hash = facts_fingerprint(facts)
    key = cache_key("trend_review", score.theme, fact_hash)

    if use_cache:
        cached = cache_get(db, key)
        if isinstance(cached, dict):
            try:
                review = TrendReview.model_validate(cached)
            except ValueError:
                review = None  # corrupt entry: fall through and re-analyze
            if review is not None:
                log.info("trend review cache hit", theme=score.theme, fact_hash=fact_hash)
                return review.model_copy(update={"from_cache": True, "llm_used": False})

    try:
        payload = complete_json(
            db,
            role=role,
            system=_SYSTEM,
            user=json.dumps(facts, default=str),
            schema=TrendReviewPayload,
            endpoint="trends.review",
        )
    except LLMError as exc:
        log.warning(
            "trend review unavailable — deterministic assessment stands",
            theme=score.theme,
            error=str(exc),
        )
        return _neutral(score.theme, fact_hash)

    review = TrendReview(
        theme=score.theme,
        verdict=payload.verdict,
        assessment=payload.assessment[:600],
        key_risks=[r[:160] for r in payload.key_risks[:3]],
        llm_used=True,
        fact_hash=fact_hash,
    )
    cache_set(db, key, review.model_dump(), TTL_LLM_REVIEW, kind="trend_review")
    return review


def apply_review(score: TrendScore, review: TrendReview) -> TrendScore:
    """Fold a review into a trend score. STRICTLY one-directional.

    Returns a copy — the deterministic score object is never mutated, so the
    pre-review value stays available for the audit trail.
    """
    multiplier = review.score_multiplier
    adjusted = min(score.score, round(score.score * multiplier, 2))
    legitimacy = "hype" if review.forces_hype_label else score.legitimacy

    evidence = dict(score.evidence)
    evidence["llm_review"] = {
        "verdict": review.verdict,
        "assessment": review.assessment,
        "key_risks": review.key_risks,
        "llm_used": review.llm_used,
        "from_cache": review.from_cache,
        "score_before_review": score.score,
        "score_after_review": adjusted,
    }
    explanation = score.explanation
    if review.assessment:
        explanation = f"{explanation} AI review ({review.verdict}): {review.assessment}"

    return score.model_copy(
        update={
            "score": adjusted,
            "legitimacy": legitimacy,
            "evidence": evidence,
            "explanation": explanation,
        }
    )
