"""End-to-end: the trend agent's hand-off to risk, portfolio and the report.

`complete_json` is always monkeypatched — no test ever performs a network call.
"""

import pytest

from sentinel.modes import set_mode
from sentinel.risk.profile import RiskProfile
from sentinel.risk.store import save_profile
from sentinel.trends import agent, allocation
from sentinel.trends import report as report_mod
from sentinel.trends import review as review_mod
from sentinel.trends.taxonomy import THEMES_BY_KEY
from tests.trend_synth import (
    seed_bars,
    seed_equity,
    seed_fundamentals,
    seed_holdings,
    seed_theme_corpus,
)

NUCLEAR = THEMES_BY_KEY["nuclear"]


@pytest.fixture()
def strong_nuclear(db):
    """A genuinely confirmed nuclear trend with investable constituents."""
    seed_equity(db, 5_000.0)
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    for symbol in NUCLEAR.seeds[:8]:
        seed_bars(db, symbol, start=60.0, drift=0.35, volume=4_000_000)
        seed_fundamentals(db, symbol, sector="Utilities", market_cap=20_000.0)
    for etf in NUCLEAR.etfs:
        seed_bars(db, etf, start=30.0, drift=0.15)
    seed_theme_corpus(db, recent_news=30, baseline_news=4, gov=8, social=4)
    seed_holdings(db, "URA", {"CCJ": 4.0}, days_ago=30)
    seed_holdings(db, "URA", {"CCJ": 9.0}, days_ago=0)
    return db


@pytest.fixture()
def no_llm(monkeypatch):
    """Any LLM call is a test failure unless a test opts in."""

    def explode(*args, **kwargs):
        raise AssertionError("unexpected LLM call")

    monkeypatch.setattr(review_mod, "complete_json", explode)
    return explode


# ------------------------------------------------------------- report ----
def test_report_generates_with_trends_and_opportunities(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    assert report.trends
    assert report.trends[0].theme == "nuclear"
    assert report.market_environment in {"Bullish", "Neutral", "Bearish"}
    assert report.opportunities
    assert all(o.approved for o in report.opportunities)


def test_free_mode_spends_nothing_and_still_reports(strong_nuclear, no_llm):
    """Free mode must be structurally incapable of making a call."""
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    assert report.llm_calls == 0
    assert report.llm_used is False
    assert report.trends  # the whole report is still produced
    assert report.portfolio


def test_smart_mode_caps_theme_reviews(strong_nuclear, monkeypatch):
    db = strong_nuclear
    set_mode(db, "smart")
    calls: list[str] = []

    def fake(db_, role, system, user, schema, endpoint=""):
        calls.append(endpoint)
        return review_mod.TrendReviewPayload(
            verdict="confirms", assessment="looks durable", key_risks=["policy reversal"]
        )

    monkeypatch.setattr(review_mod, "complete_json", fake)
    report = agent.generate_report(db)
    assert report.llm_calls <= agent.THEME_REVIEW_CAP["smart"]
    assert all(e == "trends.review" for e in calls)


def test_review_is_per_theme_never_per_ticker(strong_nuclear, monkeypatch):
    """The cost rule that must never be broken: no per-candidate fan-out."""
    db = strong_nuclear
    set_mode(db, "smart")
    calls: list[str] = []

    def fake(db_, role, system, user, schema, endpoint=""):
        calls.append(endpoint)
        return review_mod.TrendReviewPayload(verdict="confirms", assessment="ok")

    monkeypatch.setattr(review_mod, "complete_json", fake)
    report = agent.generate_report(db)
    # Far more candidates are ranked than calls are made.
    assert len(calls) <= agent.THEME_REVIEW_CAP["smart"]
    assert len(report.opportunities) + len(report.rejected) >= len(calls)


def test_llm_review_can_only_lower_a_score(strong_nuclear, monkeypatch):
    db = strong_nuclear
    set_mode(db, "smart")

    def fake(db_, role, system, user, schema, endpoint=""):
        return review_mod.TrendReviewPayload(
            verdict="hype", assessment="recycled press releases", key_risks=[]
        )

    monkeypatch.setattr(review_mod, "complete_json", fake)
    report = agent.generate_report(db)
    top = report.trends[0]
    llm = top.evidence.get("llm_review")
    assert llm is not None
    assert llm["score_after_review"] <= llm["score_before_review"]
    assert top.legitimacy == "hype"


def test_llm_outage_leaves_the_deterministic_score_untouched(strong_nuclear, monkeypatch):
    from sentinel.providers.llm.client import LLMError

    db = strong_nuclear
    set_mode(db, "smart")

    def boom(*args, **kwargs):
        raise LLMError("outage")

    monkeypatch.setattr(review_mod, "complete_json", boom)
    with_outage = agent.generate_report(db, persist=False)

    set_mode(db, "free")
    deterministic = agent.generate_report(db, persist=False)
    assert with_outage.trends[0].score == deterministic.trends[0].score


def test_apply_review_never_raises_a_score():
    """Unit-level guarantee, independent of the pipeline."""
    from sentinel.trends.scoring import TrendScore

    score = TrendScore(theme="nuclear", name="Nuclear", day=__import__("datetime").date.today(), score=70.0)
    for verdict in ("confirms", "overstated", "hype"):
        review = review_mod.TrendReview(theme="nuclear", verdict=verdict)
        assert review_mod.apply_review(score, review).score <= 70.0


# ------------------------------------------------------- risk hand-off ----
def test_every_recommendation_passed_the_risk_engine(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    for opportunity in report.opportunities:
        assert opportunity.allocation is not None
        assert opportunity.allocation.risk_check is not None
        assert opportunity.allocation.risk_check.approved


def test_risk_veto_blocks_a_recommendation_regardless_of_trend_score(
    strong_nuclear, no_llm
):
    """A theme scoring 100 buys nothing if the engine says no."""
    db = strong_nuclear
    # One open position allowed, and none free — every BUY must fail.
    save_profile(db, RiskProfile(version=2, max_open_positions=1))
    from sentinel.db.models import Position

    db.add(Position(symbol="AAPL", shares=10, cost_basis=100))
    db.flush()

    set_mode(db, "free")
    report = agent.generate_report(db)
    assert report.opportunities == []
    assert report.rejected
    assert any(
        "max_open_positions" in (o.allocation.failed_rules if o.allocation else [])
        for o in report.rejected
    )


def test_dollars_never_exceed_available_cash(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    cash = report.portfolio["cash"]
    for opportunity in report.opportunities:
        assert opportunity.dollars <= cash + 0.01


def test_basket_exposure_accumulates_across_recommendations(strong_nuclear, no_llm):
    """Five names that each pass in isolation must not together breach the
    limits. The report is a basket the user might act on in one sitting."""
    db = strong_nuclear
    set_mode(db, "free")
    # All the seeded names share one sector, so a 25% sector cap binds on the
    # basket long before it binds on any single position.
    save_profile(db, RiskProfile(version=2, max_sector_pct=25.0, max_position_pct=10.0))
    report = agent.generate_report(db)

    equity = report.portfolio["equity"]
    total = sum(o.dollars for o in report.opportunities)
    assert total <= equity * 0.25 + 0.01

    # And the binding constraint must be a BASKET-level rule: later candidates
    # are refused because of what was already recommended above them, not
    # because of anything wrong with the candidate itself.
    basket_rules = {"max_sector_pct", "max_correlated_exposure", "max_portfolio_exposure_pct"}
    assert any(
        basket_rules & set(o.allocation.failed_rules)
        for o in report.rejected
        if o.allocation is not None
    )


def test_cumulative_basket_never_exceeds_cash(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    assert sum(o.dollars for o in report.opportunities) <= report.portfolio["cash"] + 0.01


def test_with_proposed_leaves_the_real_state_untouched(strong_nuclear, no_llm):
    """The working state is a copy; nothing is persisted or mutated."""
    from sentinel.portfolio.state import build_portfolio_state

    db = strong_nuclear
    before = build_portfolio_state(db)
    opportunity = agent.Opportunity(
        symbol="CCJ",
        sector="Utilities",
        price=50.0,
        allocation=allocation.Allocation(
            symbol="CCJ", approved=True, dollars=500.0, shares=10, price=50.0
        ),
    )
    updated, cash = agent._with_proposed(before, 1_000.0, opportunity)
    assert len(updated.positions) == len(before.positions) + 1
    assert cash == 500.0
    assert build_portfolio_state(db).positions == before.positions


def test_report_includes_portfolio_context(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    portfolio = report.portfolio
    for key in ("equity", "cash", "open_positions", "sector_exposure", "max_sector_pct"):
        assert key in portfolio


def test_a_name_is_never_recommended_twice_across_themes(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    symbols = [o.symbol for o in report.opportunities]
    assert len(symbols) == len(set(symbols))


# --------------------------------------------------------- allocation ----
def test_allocation_declines_without_price_history(db):
    seed_equity(db)
    result = allocation.allocate(db, "XXX", 50.0, None, "Energy")
    assert not result.approved
    assert "not enough price history" in result.reasons[0]


def test_allocation_is_trimmed_to_cash(db):
    from sentinel.agents.technicals import compute_technicals
    from sentinel.data.context import build_market_context

    seed_equity(db, 200.0)  # tiny account
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.1)
    seed_fundamentals(db, "CCJ")
    context = build_market_context(db, ["CCJ"])
    snap = compute_technicals("CCJ", context.symbols["CCJ"].daily_bars)
    result = allocation.allocate(db, "CCJ", snap.close, snap, "Energy")
    assert result.dollars <= 200.0


def test_tiny_allocation_is_refused_rather_than_rounded_up(db):
    from sentinel.agents.technicals import compute_technicals
    from sentinel.data.context import build_market_context

    seed_equity(db, 20.0)
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.1)
    seed_fundamentals(db, "CCJ")
    context = build_market_context(db, ["CCJ"])
    snap = compute_technicals("CCJ", context.symbols["CCJ"].daily_bars)
    result = allocation.allocate(db, "CCJ", snap.close, snap, "Energy")
    assert not result.approved
    assert result.dollars == 0.0


# ------------------------------------------------------------ message ----
def test_message_has_the_requested_sections(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    text = report_mod.compose(agent.generate_report(db))
    assert "B-Quant Trend Report" in text
    assert "Market Environment:" in text
    assert "Top Emerging Trends:" in text
    assert "Strength Score:" in text
    assert "Best Opportunities:" in text
    assert "Not financial advice" in text


def test_message_uses_dollars_not_percentages(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    text = report_mod.compose(report)
    if report.opportunities:
        assert "Purchase Amount: $" in text
        assert "Suggested Buys:" in text
        assert "BUY " in text


def test_message_shows_the_bear_case(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    report = agent.generate_report(db)
    text = report_mod.compose(report)
    assert "Risk Level:" in text
    assert "Confidence Score:" in text


def test_message_survives_an_empty_report(db, no_llm):
    seed_equity(db)
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    set_mode(db, "free")
    text = report_mod.compose(agent.generate_report(db))
    assert "B-Quant Trend Report" in text
    assert "Not financial advice" in text


def test_report_is_persisted_and_retrievable(strong_nuclear, no_llm):
    db = strong_nuclear
    set_mode(db, "free")
    generated = agent.generate_report(db)
    report_mod.send_report(db, generated)
    row = agent.latest_report(db)
    assert row is not None
    assert row.day == generated.day
    assert "B-Quant Trend Report" in row.text
