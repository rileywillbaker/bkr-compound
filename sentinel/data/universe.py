"""Trading universe: a large static ticker list, NOT the watchlist.

The default universe is the S&P 500, stored in config/universe_sp500.csv
(one ticker per line, first column; editable without code changes — refresh
it when index membership churns). The Settings watchlist still exists but
only as "highlighted tickers" for the UI and briefs; it never limits which
symbols the screener, analysts, strategy selector, or risk engine may
operate on. Any universe ticker (or an explicit on-demand ticker, e.g. chat
"Should I buy XYZ?") can produce a BUY/SELL/NO TRADE signal, and every
signal still passes through the pure-Python risk gate.
"""

from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from sentinel.config import PROJECT_ROOT

MARKET_SYMBOLS = ["SPY"]  # always ingested; regime detection needs it

UNIVERSE_CSV = PROJECT_ROOT / "config" / "universe_sp500.csv"


@lru_cache
def load_static_universe(path: Path | None = None) -> tuple[str, ...]:
    """Tickers from the universe CSV (header/comment lines ignored)."""
    symbols: set[str] = set()
    with open(path or UNIVERSE_CSV, encoding="utf-8") as fh:
        for line in fh:
            sym = line.split(",")[0].strip().upper()
            if not sym or sym == "SYMBOL" or sym.startswith("#"):
                continue
            symbols.add(sym)
    return tuple(sorted(symbols))


def get_universe(db: Session) -> list[str]:
    """Full scan/ingest universe: static list + highlighted watchlist +
    held positions + market symbols. Positions are always included so exits
    keep being monitored even if a name drops out of the static list."""
    from sentinel.db.models import Position
    from sentinel.db.settings_store import get_watchlist

    symbols = set(load_static_universe()) | set(MARKET_SYMBOLS) | set(get_watchlist(db))
    for (sym,) in db.query(Position.symbol).filter(Position.shares != 0).all():
        symbols.add(sym)
    return sorted(symbols)


def held_symbols(db: Session) -> list[str]:
    from sentinel.db.models import Position

    return sorted(
        sym for (sym,) in db.query(Position.symbol).filter(Position.shares != 0).all()
    )
