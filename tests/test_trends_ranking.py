"""Stock ranking: the quality gate, the pump guard, and the seven factors.

The rule under test throughout: being in the path of a trend is necessary but
never sufficient.
"""

from datetime import UTC, date, datetime, timedelta

from sentinel.agents.technicals import compute_technicals
from sentinel.db.models import EarningsCalendarRow, FilingRow
from sentinel.trends import market, ranking
from sentinel.trends.taxonomy import THEMES_BY_KEY
from tests.trend_synth import seed_bars, seed_fundamentals, seed_holdings, seed_social

URANIUM = THEMES_BY_KEY["uranium"]


def _rank(db, symbols, **kwargs):
    series = market.load_series(db, set(symbols) | {"SPY"})
    benchmark = [c for c, _ in series.get("SPY", [])]
    snapshots = {}
    from sentinel.data.context import build_market_context

    context = build_market_context(db, list(symbols))
    for symbol, sym_ctx in context.symbols.items():
        if sym_ctx.daily_bars:
            snapshots[symbol] = compute_technicals(symbol, sym_ctx.daily_bars)
    return ranking.rank_theme_stocks(
        db, URANIUM, list(symbols), series, benchmark, snapshots=snapshots, **kwargs
    )


# -------------------------------------------------------- quality gate ----
def test_penny_stock_is_excluded(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "UEC", start=1.0, drift=0.001, volume=50_000_000)
    seed_fundamentals(db, "UEC", market_cap=800.0)
    ranked, excluded = _rank(db, ["UEC"])
    assert ranked == []
    assert "penny-stock floor" in excluded[0].exclusion_reason


def test_illiquid_stock_is_excluded(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "UEC", start=40.0, drift=0.1, volume=1_000)  # ~$40k/day
    seed_fundamentals(db, "UEC")
    ranked, excluded = _rank(db, ["UEC"])
    assert ranked == []
    assert "liquidity floor" in excluded[0].exclusion_reason


def test_micro_cap_is_excluded(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "UEC", start=40.0, drift=0.1)
    seed_fundamentals(db, "UEC", market_cap=120.0)
    ranked, excluded = _rank(db, ["UEC"])
    assert ranked == []
    assert "market cap" in excluded[0].exclusion_reason


def test_missing_fundamentals_fails_closed(db):
    """Unlike discovery, this stage attaches a dollar amount — so unknown
    company data must block the recommendation, not pass it through."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "UEC", start=40.0, drift=0.1)
    ranked, excluded = _rank(db, ["UEC"])
    assert ranked == []
    assert "no fundamentals" in excluded[0].exclusion_reason


def test_insufficient_history_is_excluded(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "UEC", n=40, start=40.0, drift=0.1)
    seed_fundamentals(db, "UEC")
    ranked, excluded = _rank(db, ["UEC"])
    assert ranked == []
    assert "not enough history" in excluded[0].exclusion_reason


def test_unknown_sector_is_excluded(db):
    """The risk engine's concentration rules need a sector."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "UEC", start=40.0, drift=0.1)
    seed_fundamentals(db, "UEC", sector="")
    ranked, excluded = _rank(db, ["UEC"])
    assert ranked == []
    assert "sector unknown" in excluded[0].exclusion_reason


def test_quality_name_passes_the_gate(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.15)
    seed_fundamentals(db, "CCJ", name="Cameco Corporation")
    ranked, excluded = _rank(db, ["CCJ"])
    assert [s.symbol for s in ranked] == ["CCJ"]
    assert excluded == []


# ---------------------------------------------------------- pump guard ----
def _seed_pump(db, symbol="DNN"):
    """A violent spike on huge volume in a small, heavily-shorted, loudly
    discussed name — with no earnings or filing behind it."""
    from sentinel.db.models import ShortInterestRow

    seed_bars(db, "SPY", start=400.0, drift=0.05)
    # Flat for months, then a near-doubling in the last 21 sessions.
    seed_bars(db, symbol, n=240, start=10.0, drift=0.0, end=datetime.now(UTC) - timedelta(days=21))
    seed_bars(db, symbol, n=21, start=10.0, drift=0.5, volume=30_000_000, last_volume=90_000_000)
    seed_fundamentals(db, symbol, market_cap=900.0, pe=None, revenue_growth=None)
    db.add(
        ShortInterestRow(symbol=symbol, as_of=date.today(), short_percent_float=28.0)
    )
    seed_social(db, symbol, mentions=60)
    db.flush()


def test_pump_signature_is_excluded(db):
    _seed_pump(db)
    ranked, excluded = _rank(db, ["DNN"])
    assert ranked == []
    assert any("possible pump" in s.exclusion_reason for s in excluded)


def test_a_real_catalyst_defuses_the_pump_guard(db):
    """The same price action with an 8-K behind it is not a pump — it is news."""
    _seed_pump(db)
    db.add(
        FilingRow(
            accession_no="0001",
            symbol="DNN",
            cik="123",
            form="8-K",
            filed_at=date.today() - timedelta(days=2),
            description="Entry into a material definitive agreement",
        )
    )
    db.flush()
    ranked, excluded = _rank(db, ["DNN"])
    assert [s.symbol for s in ranked] == ["DNN"]
    assert not any("possible pump" in s.exclusion_reason for s in excluded)


def test_earnings_also_defuses_the_pump_guard(db):
    _seed_pump(db)
    db.add(
        EarningsCalendarRow(
            symbol="DNN",
            date=date.today() - timedelta(days=3),
            eps_actual=0.4,
            eps_estimate=0.1,
        )
    )
    db.flush()
    ranked, _ = _rank(db, ["DNN"])
    assert [s.symbol for s in ranked] == ["DNN"]


def test_a_strong_stock_rising_normally_is_not_a_pump(db):
    """Plenty of good stocks go up. Only the full signature should trip."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.25)
    seed_fundamentals(db, "CCJ", market_cap=25_000.0)
    ranked, excluded = _rank(db, ["CCJ"])
    assert [s.symbol for s in ranked] == ["CCJ"]
    assert excluded == []


# ------------------------------------------------------------- factors ----
def test_stronger_company_ranks_above_weaker_one(db):
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    # Good: growing, profitable, outperforming, large.
    seed_bars(db, "CCJ", start=40.0, drift=0.3)
    seed_fundamentals(db, "CCJ", market_cap=30_000.0, pe=22.0, ps=5.0, revenue_growth=25.0)
    # Weak: shrinking, unprofitable, lagging, small.
    seed_bars(db, "URG", start=40.0, drift=0.01)
    seed_fundamentals(db, "URG", market_cap=900.0, pe=-5.0, ps=20.0, revenue_growth=-8.0)
    # A third name so the peer percentile ranks have a population.
    seed_bars(db, "NXE", start=40.0, drift=0.1)
    seed_fundamentals(db, "NXE", market_cap=5_000.0, pe=40.0, ps=10.0, revenue_growth=5.0)

    ranked, _ = _rank(db, ["CCJ", "URG", "NXE"])
    order = [s.symbol for s in ranked]
    assert order.index("CCJ") < order.index("URG")


def test_missing_data_lowers_confidence_below_composite(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.15)
    seed_fundamentals(db, "CCJ", pe=None, revenue_growth=None)
    ranked, _ = _rank(db, ["CCJ"])
    stock = ranked[0]
    assert stock.data_gaps
    assert stock.confidence < stock.composite


def test_etf_holdings_raise_institutional_interest(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.15)
    seed_fundamentals(db, "CCJ")
    without, _ = _rank(db, ["CCJ"])

    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=0)
    with_etf, _ = _rank(db, ["CCJ"])
    assert (
        with_etf[0].factors.institutional_interest
        > without[0].factors.institutional_interest
    )


def test_risk_level_reflects_volatility_and_size(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=200.0, drift=0.05)  # low volatility, large
    seed_fundamentals(db, "CCJ", market_cap=60_000.0, beta=0.9)
    seed_bars(db, "UEC", n=260, start=10.0, drift=0.3, volume=40_000_000)  # jumpy, small
    seed_fundamentals(db, "UEC", market_cap=1_200.0, beta=2.2)
    ranked, _ = _rank(db, ["CCJ", "UEC"])
    by_symbol = {s.symbol: s for s in ranked}
    assert by_symbol["UEC"].factors.risk > by_symbol["CCJ"].factors.risk


def test_valuation_is_ranked_within_peers_not_absolutely(db):
    """A 40x semiconductor and a 40x utility are not the same statement, so
    valuation must be relative to the theme's own peer set."""
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    for symbol, pe in (("CCJ", 30.0), ("NXE", 60.0), ("UUUU", 90.0), ("DNN", 120.0)):
        seed_bars(db, symbol, start=40.0, drift=0.1)
        seed_fundamentals(db, symbol, pe=pe, ps=pe / 5)
    ranked, _ = _rank(db, ["CCJ", "NXE", "UUUU", "DNN"])
    by_symbol = {s.symbol: s for s in ranked}
    # 30x is expensive in absolute terms but the cheapest of this peer group.
    assert by_symbol["CCJ"].factors.valuation > by_symbol["DNN"].factors.valuation


def test_bullish_and_bearish_points_are_both_produced(db):
    seed_bars(db, "SPY", start=400.0, drift=0.5)
    seed_bars(db, "URG", start=40.0, drift=0.01)  # lagging badly
    seed_fundamentals(db, "URG", pe=-4.0, revenue_growth=-12.0, market_cap=900.0)
    ranked, _ = _rank(db, ["URG"])
    assert ranked[0].bearish
    assert any("not currently profitable" in b for b in ranked[0].bearish)


def test_trend_connection_is_explained(db):
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.15)
    seed_fundamentals(db, "CCJ")
    seed_holdings(db, "URA", {"CCJ": 20.0}, days_ago=0)
    ranked, _ = _rank(db, ["CCJ"], theme_mentions={"CCJ": 4})
    connection = ranked[0].trend_connection.lower()
    assert "thematic etf" in connection
    assert "article" in connection


def test_ranking_makes_no_llm_calls(db, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("ranking must not call an LLM")

    monkeypatch.setattr("sentinel.providers.llm.client.complete_json", explode)
    seed_bars(db, "SPY", start=400.0, drift=0.1)
    seed_bars(db, "CCJ", start=40.0, drift=0.15)
    seed_fundamentals(db, "CCJ")
    ranked, _ = _rank(db, ["CCJ"])
    assert ranked
