"""The single combined LLM review: caching, fingerprinting, safety limits.

`complete_json` is always monkeypatched — no test ever performs a network call.
"""

import pytest

from sentinel.agents import review as review_mod
from sentinel.agents.regime import RegimeAssessment
from sentinel.agents.review import (
    STANCE_CONFIDENCE_MULTIPLIER,
    CandidateReview,
    LLMReviewPayload,
    build_fact_pack,
    facts_fingerprint,
    review_candidate,
)
from sentinel.agents.screener import ScreenResult
from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.data.context import SymbolContext
from sentinel.providers.llm.client import LLMError

REGIME = RegimeAssessment(regime="bull-trend", confidence=0.8, detail="synthetic")


def snapshot(**kw) -> TechnicalSnapshot:
    base = dict(
        symbol="NVDA",
        close=100.0,
        rsi14=58.0,
        macd_hist=0.5,
        atr_pct=2.0,
        above_sma20=True,
        above_sma50=True,
        above_sma200=True,
        relative_volume=1.4,
        pct_from_52w_high=-2.0,
        avg_dollar_volume20=50_000_000.0,
        bars_used=250,
    )
    base.update(kw)
    return TechnicalSnapshot(**base)


def facts(**kw) -> dict:
    return build_fact_pack(
        symbol="NVDA",
        snap=kw.pop("snap", snapshot()),
        screen=ScreenResult(symbol="NVDA", eligible=True, momentum_score=40, trend_score=100),
        sym_ctx=kw.pop(
            "sym_ctx",
            SymbolContext(
                symbol="NVDA", daily_bars=[], news=[], sector="Technology", market_cap=90_000
            ),
        ),
        verdicts=[],
        regime=REGIME,
        strategy="momentum-swing",
        action="BUY",
        sizing=None,
        risk_check=None,
        base_confidence=0.55,
        **kw,
    )


@pytest.fixture()
def fake_llm(monkeypatch):
    calls: list[str] = []

    def fake(db, role, system, user, schema, endpoint=""):
        calls.append(endpoint)
        return LLMReviewPayload(
            stance="confirm", explanation="Because the trend is intact.", key_risks=["gap risk"]
        )

    monkeypatch.setattr(review_mod, "complete_json", fake)
    return calls


def test_review_calls_the_model_once_and_caches(db, fake_llm):
    first = review_candidate(db, "NVDA", facts(), trigger="breakout")
    assert first.llm_used and not first.from_cache
    assert first.explanation == "Because the trend is intact."
    assert first.key_risks == ["gap risk"]

    second = review_candidate(db, "NVDA", facts(), trigger="breakout")
    assert second.from_cache and not second.llm_used
    assert second.explanation == first.explanation
    assert len(fake_llm) == 1  # the second look was free


def test_material_change_busts_the_cache(db, fake_llm):
    review_candidate(db, "NVDA", facts())
    changed = facts(snap=snapshot(above_sma50=False, above_sma200=False))
    review_candidate(db, "NVDA", changed)
    assert len(fake_llm) == 2


def test_new_headlines_bust_the_cache(db, fake_llm):
    from datetime import UTC, datetime

    from sentinel.providers.types import NewsItem

    review_candidate(db, "NVDA", facts())
    with_news = facts(
        sym_ctx=SymbolContext(
            symbol="NVDA",
            daily_bars=[],
            sector="Technology",
            market_cap=90_000,
            news=[
                NewsItem(
                    provider_id="n1",
                    symbol="NVDA",
                    headline="Regulator opens investigation",
                    source="wire",
                    published_at=datetime.now(UTC),
                )
            ],
        )
    )
    review_candidate(db, "NVDA", with_news)
    assert len(fake_llm) == 2


def test_fingerprint_ignores_noise_but_catches_signal():
    """A cent of drift is not news; losing the 200-day average is."""
    quiet = facts(snap=snapshot(close=100.02, rsi14=58.4))
    assert facts_fingerprint(facts()) == facts_fingerprint(quiet)

    broken = facts(snap=snapshot(above_sma200=False))
    assert facts_fingerprint(facts()) != facts_fingerprint(broken)


def test_llm_outage_degrades_to_a_neutral_review(db, monkeypatch):
    def boom(*args, **kwargs):
        raise LLMError("outage")

    monkeypatch.setattr(review_mod, "complete_json", boom)
    review = review_candidate(db, "NVDA", facts(), trigger="breakout")
    assert not review.llm_used
    assert review.stance == "confirm"  # neutral: the deterministic verdict stands
    assert review.explanation == ""  # no narrative → caller uses its template
    assert review.confidence_multiplier == 1.0


def test_stance_multipliers_can_only_hold_or_lower():
    """The categorical stance is the ONLY thing the model decides here, and the
    mapping is one-directional by construction."""
    assert STANCE_CONFIDENCE_MULTIPLIER["confirm"] == 1.0
    assert all(m <= 1.0 for m in STANCE_CONFIDENCE_MULTIPLIER.values())
    assert STANCE_CONFIDENCE_MULTIPLIER["reject"] == 0.0
    assert CandidateReview(symbol="X", stance="reject").vetoes_trade
    assert not CandidateReview(symbol="X", stance="caution").vetoes_trade


def test_fact_pack_carries_only_precomputed_numbers():
    pack = facts()
    assert pack["proposed_action"] == "BUY"
    assert pack["technicals"]["rsi14"] == 58.0
    assert pack["fundamentals"]["sector"] == "Technology"
    assert "computed_levels" in pack and "risk_engine" in pack
