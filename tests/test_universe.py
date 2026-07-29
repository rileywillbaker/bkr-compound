"""Static universe: CSV loads, watchlist is highlight-only, positions and
watchlist names are always included in the ingest universe."""

import re

from sentinel.data.universe import (
    get_universe,
    held_symbols,
    load_static_universe,
    universe_files,
)
from sentinel.db.models import Position
from sentinel.db.settings_store import set_watchlist

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def test_static_universe_is_large_and_clean():
    universe = load_static_universe()
    assert len(universe) >= 400  # S&P 500 scale, not a watchlist
    assert len(universe) == len(set(universe))
    assert all(_TICKER_RE.match(s) for s in universe)
    for known in ("AAPL", "NVDA", "MSFT", "XOM", "JPM", "BRK.B"):
        assert known in universe


def test_universe_unions_every_csv():
    """Screening is pure arithmetic over stored bars, so a wider universe costs
    CPU and nothing else — hence three source lists, not one."""
    names = {p.name for p in universe_files()}
    assert {
        "universe_sp500.csv",
        "universe_nasdaq100.csv",
        "universe_liquid_largecap.csv",
    } <= names

    universe = load_static_universe()
    assert len(universe) > 550  # meaningfully wider than the S&P 500 alone
    # names that exist only outside the S&P 500 file
    for outside in ("TSM", "SHOP", "ARM", "PDD"):
        assert outside in universe


def test_universe_files_are_deduplicated(tmp_path):
    """Heavy overlap between the index files is expected and must not produce
    duplicate work."""
    csv = tmp_path / "universe_test.csv"
    csv.write_text("symbol\nAAPL\nAAPL\n# comment\n\nMSFT\n", encoding="utf-8")
    assert load_static_universe(csv) == ("AAPL", "MSFT")


def test_universe_includes_watchlist_and_positions_and_spy(db):
    set_watchlist(db, ["ZZZC"])  # a highlight OUTSIDE the static list
    db.add(Position(symbol="ZZZP", shares=10, cost_basis=5))
    db.flush()
    universe = get_universe(db)
    assert "SPY" in universe  # regime symbol
    assert "ZZZC" in universe  # highlighted tickers are always covered
    assert "ZZZP" in universe  # held positions are always covered
    assert "AAPL" in universe  # static list survives a tiny watchlist
    assert len(universe) >= 400


def test_watchlist_does_not_limit_universe(db):
    set_watchlist(db, ["NVDA"])
    assert len(get_universe(db)) >= 400


def test_held_symbols(db):
    db.add(Position(symbol="NVDA", shares=3, cost_basis=100))
    db.add(Position(symbol="OLD", shares=0, cost_basis=50))  # closed
    db.flush()
    assert held_symbols(db) == ["NVDA"]
