"""Trend strength scoring, coverage handling and the hype guard."""

from datetime import date, timedelta

from sentinel.trends import market, scoring
from sentinel.trends.taxonomy import THEMES_BY_KEY
from tests.trend_synth import (
    seed_bars,
    seed_document,
    seed_fundamentals,
    seed_holdings,
    seed_social,
    seed_theme_corpus,
)

NUCLEAR = THEMES_BY_KEY["nuclear"]
URANIUM = THEMES_BY_KEY["uranium"]


def _series(db, symbols):
    return market.load_series(db, set(symbols) | {"SPY"})


def _benchmark(series):
    return [c for c, _ in series.get("SPY", [])]


def _score(db, theme=NUCLEAR, **kwargs):
    series = _series(db, [*theme.seeds, *theme.etfs])
    return scoring.score_theme(
        db, theme, scoring.build_corpus(db), series, _benchmark(series), **kwargs
    )


# --------------------------------------------------------- components ----
def test_theme_with_no_data_scores_low(db):
    seed_bars(db, "SPY", start=400.0, drift=0.3)
    result = _score(db)
    assert result.score < 40
    assert result.legitimacy in {"unproven", "hype", "mixed"}


def test_accelerating_news_lifts_the_score(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    quiet = _score(db)
    seed_theme_corpus(db)
    loud = _score(db)
    news = loud.component("news_momentum")
    assert news is not None and news.score > 0
    assert loud.score > quiet.score


def test_market_confirmation_reflects_outperformance(db):
    seed_bars(db, "SPY", start=400.0, drift=0.05)  # market barely moves
    for symbol in NUCLEAR.seeds[:6]:
        seed_bars(db, symbol, start=50.0, drift=0.6)  # constituents run
    result = _score(db)
    confirmation = result.component("market_confirmation")
    assert confirmation is not None
    assert confirmation.covered
    assert confirmation.score > 60


def test_underperforming_basket_scores_below_neutral(db):
    seed_bars(db, "SPY", start=400.0, drift=0.8)
    for symbol in NUCLEAR.seeds[:6]:
        seed_bars(db, symbol, start=50.0, drift=-0.05)
    result = _score(db)
    confirmation = result.component("market_confirmation")
    assert confirmation is not None and confirmation.score < 50


def test_unmeasured_component_is_not_scored_as_zero(db):
    """An unreachable source must lower confidence, not silently score 0."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_theme_corpus(db, gov=0, social=0)
    result = _score(db, gov_covered=False, social_covered=False)
    policy = result.component("policy_support")
    social = result.component("social_attention")
    assert policy is not None and not policy.covered
    assert social is not None and not social.covered
    assert "policy_support" in result.coverage_gaps
    # The remaining components still carry the full weight between them.
    assert result.score > 0


def test_covered_but_empty_is_different_from_uncovered(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_theme_corpus(db, gov=0)
    result = _score(db, gov_covered=True)
    policy = result.component("policy_support")
    assert policy is not None
    assert policy.covered  # we looked
    assert policy.score == 0.0  # and found nothing


def test_policy_themes_weight_government_evidence_higher(db):
    """A policy-driven theme reallocates weight from chatter to government."""
    weights_policy = scoring._weights_for(NUCLEAR)
    weights_normal = scoring._weights_for(THEMES_BY_KEY["ai"])
    assert weights_policy["policy_support"] > weights_normal["policy_support"]
    assert weights_policy["social_attention"] < weights_normal["social_attention"]


# --------------------------------------------------------- hype guard ----
def test_social_hype_without_confirmation_is_capped(db):
    """Loud discussion, no market/policy/ETF backing → capped and flagged."""
    seed_bars(db, "SPY", start=400.0, drift=0.5)
    for symbol in URANIUM.seeds[:5]:
        seed_bars(db, symbol, start=20.0, drift=-0.02)  # going nowhere
    for i in range(60):
        seed_document(
            db,
            key=f"hype-{i}",
            title=f"$UEC uranium to the moon \U0001F680 ({i})",
            channel="social",
            source="reddit:wallstreetbets",
            themes=["uranium"],
            symbols=["UEC"],
            days_ago=1.0,
            engagement=500,
        )
    seed_social(db, "UEC", mentions=200, day_offset=0)
    result = _score(db, theme=URANIUM)
    assert result.hype_flags
    assert result.legitimacy in {"hype", "mixed"}
    assert result.score <= result.raw_score


def test_confirmed_trend_is_not_flagged_as_hype(db):
    """News + policy + a genuinely outperforming basket → legitimate."""
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    for symbol in NUCLEAR.seeds[:8]:
        seed_bars(db, symbol, start=50.0, drift=0.7)
        seed_fundamentals(db, symbol)
    seed_theme_corpus(db, recent_news=30, baseline_news=4, gov=8, social=4)
    result = _score(db)
    assert result.score >= 55
    assert result.legitimacy in {"legitimate", "emerging"}


def test_single_stock_concentration_is_flagged(db):
    """One name carrying the whole basket is a stock story, not a theme."""
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    seeds = list(NUCLEAR.seeds[:6])
    seed_bars(db, seeds[0], start=20.0, drift=1.5)  # the one that ran
    for symbol in seeds[1:]:
        seed_bars(db, symbol, start=50.0, drift=0.0)  # flat
    result = _score(db)
    assert any("accounts for" in flag or "narrow" in flag for flag in result.hype_flags)


def test_legitimacy_cap_only_lowers(db):
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    for symbol in NUCLEAR.seeds[:8]:
        seed_bars(db, symbol, start=50.0, drift=0.7)
    seed_theme_corpus(db, recent_news=30, baseline_news=4, gov=8)
    result = _score(db)
    assert result.score <= result.raw_score


# ------------------------------------------------------ ETF accumulation ----
def test_etf_accumulation_detects_weight_increase(db):
    seed_holdings(db, "URA", {"CCJ": 20.0, "UEC": 4.0}, days_ago=30)
    seed_holdings(db, "URA", {"CCJ": 20.1, "UEC": 7.5}, days_ago=0)
    accumulation = scoring.etf_accumulation(db, ["URA"])
    symbols = {a.symbol for a in accumulation}
    assert "UEC" in symbols  # +3.5 points of weight on a 4.0 base
    assert "CCJ" not in symbols  # +0.1 on a 20.0 base is price drift


def test_etf_accumulation_catches_conviction_in_a_small_holding(db):
    """0.4% → 0.9% of a fund is a real decision, but only +0.5pp absolute."""
    seed_holdings(db, "URA", {"DNN": 0.4}, days_ago=30)
    seed_holdings(db, "URA", {"DNN": 0.9}, days_ago=0)
    assert {a.symbol for a in scoring.etf_accumulation(db, ["URA"])} == {"DNN"}


def test_etf_accumulation_ignores_price_drift_in_a_large_holding(db):
    """A 20% weight wandering to 20.2% is the market, not the manager."""
    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=30)
    seed_holdings(db, "URA", {"CCJ": 20.2}, days_ago=0)
    assert scoring.etf_accumulation(db, ["URA"]) == []


def test_etf_accumulation_flags_new_additions(db):
    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=30)
    seed_holdings(db, "URA", {"CCJ": 20.0, "NXE": 3.0}, days_ago=0)
    accumulation = scoring.etf_accumulation(db, ["URA"])
    new = [a for a in accumulation if a.newly_added]
    assert [a.symbol for a in new] == ["NXE"]


def test_etf_accumulation_needs_two_snapshots(db):
    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=0)
    assert scoring.etf_accumulation(db, ["URA"]) == []


def test_no_holdings_data_is_not_treated_as_no_accumulation(db):
    """Empty must mean 'we have no free holdings data', and the ETF component
    must say so rather than implying an absence of buying."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    for etf_ticker in NUCLEAR.etfs:
        seed_bars(db, etf_ticker, start=30.0, drift=0.2)
    result = _score(db)
    component = result.component("etf_activity")
    assert component is not None
    assert "no free holdings data" in component.detail


def test_one_snapshot_is_distinguished_from_no_holdings(db):
    """Accumulation is a DIFF. Having today's holdings but no prior snapshot
    is a third state, and must not read as 'no data' or 'no buying'."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    for etf_ticker in URANIUM.etfs:
        seed_bars(db, etf_ticker, start=30.0, drift=0.2)
    seed_holdings(db, "URA", {"CCJ": 20.0, "UEC": 4.0}, days_ago=0)

    result = _score(db, theme=URANIUM)
    component = result.component("etf_activity")
    assert component is not None
    assert "needs a second snapshot" in component.detail


def test_two_snapshots_with_no_increase_says_so(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    for etf_ticker in URANIUM.etfs:
        seed_bars(db, etf_ticker, start=30.0, drift=0.2)
    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=30)
    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=0)

    result = _score(db, theme=URANIUM)
    component = result.component("etf_activity")
    assert component is not None
    assert "no meaningful increase" in component.detail


# ---------------------------------------------------------- persistence ----
def test_persistence_bonus_requires_history(db):
    assert scoring.persistence_bonus(db, "nuclear", date.today(), 70.0) == 0.0


def test_persistence_bonus_rewards_a_sustained_score(db):
    from sentinel.db.models import TrendSnapshotRow

    for offset in range(1, 6):
        db.add(
            TrendSnapshotRow(
                theme="nuclear",
                day=date.today() - timedelta(days=offset),
                score=70.0,
                legitimacy="legitimate",
            )
        )
    db.flush()
    bonus = scoring.persistence_bonus(db, "nuclear", date.today(), 70.0)
    assert 0 < bonus <= 5.0


# -------------------------------------------------------------- driver ----
def test_score_all_persists_and_ranks(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    for symbol in NUCLEAR.seeds[:6]:
        seed_bars(db, symbol, start=50.0, drift=0.6)
    seed_theme_corpus(db, recent_news=25, gov=6)
    results = scoring.score_all(db)
    assert results
    assert results == sorted(results, key=lambda r: -r.score)
    stored = scoring.latest_snapshots(db)
    assert stored
    assert stored[0].theme == results[0].theme


def test_score_all_makes_no_llm_calls(db, monkeypatch):
    """The whole scoring stage runs over every theme — it must never call out."""
    def explode(*args, **kwargs):
        raise AssertionError("scoring must not call an LLM")

    monkeypatch.setattr("sentinel.providers.llm.client.complete_json", explode)
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_theme_corpus(db)
    assert scoring.score_all(db)


def test_symbol_theme_alignment_respects_threshold(db):
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    for symbol in NUCLEAR.seeds[:8]:
        seed_bars(db, symbol, start=50.0, drift=0.7)
        seed_fundamentals(db, symbol)
    seed_theme_corpus(db, recent_news=30, baseline_news=4, gov=8)
    scoring.score_all(db)
    generous = scoring.symbol_theme_alignment(db, min_score=1.0)
    strict = scoring.symbol_theme_alignment(db, min_score=99.0)
    assert generous
    assert strict == {}


def test_single_news_mention_does_not_join_a_theme(db):
    """Observed live: one passing article reference pulled Comcast into
    "defense", where it then ranked top on a cheap P/E. One mention is
    co-occurrence, not membership."""
    seed_document(
        db,
        key="defense-incidental",
        title="Pentagon official appears on CMCSA-owned network to discuss munitions",
        themes=["defense"],
        symbols=["CMCSA"],
        days_ago=1.0,
    )
    symbols = scoring.theme_symbols(db, THEMES_BY_KEY["defense"], scoring.build_corpus(db))
    assert "CMCSA" not in symbols


def test_repeated_news_mentions_do_join_a_theme(db):
    """The discovery path must stay open: a genuine new beneficiary gets
    written about more than once."""
    for i in range(scoring.MIN_NEWS_MENTIONS):
        seed_document(
            db,
            key=f"defense-real-{i}",
            title=f"Company wins Pentagon munitions contract award ({i})",
            themes=["defense"],
            symbols=["CMCSA"],
            days_ago=1.0,
        )
    symbols = scoring.theme_symbols(db, THEMES_BY_KEY["defense"], scoring.build_corpus(db))
    assert "CMCSA" in symbols


def test_seeds_never_need_news_mentions(db):
    """Seed membership is membership regardless of coverage."""
    symbols = scoring.theme_symbols(db, THEMES_BY_KEY["defense"], scoring.build_corpus(db))
    assert "LMT" in symbols


def test_theme_symbols_exclude_tracked_etfs(db):
    """ETFs are flow evidence and must never become stock candidates."""
    seed_holdings(db, "URA", {"CCJ": 20.0, "URA": 1.0, "NXE": 3.0}, days_ago=0)
    symbols = scoring.theme_symbols(db, URANIUM, scoring.build_corpus(db))
    assert "URA" not in symbols
