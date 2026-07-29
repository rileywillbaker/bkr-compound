"""Trading universe: a large static ticker list, NOT the watchlist.

The universe is the UNION of every `config/universe_*.csv` file (one ticker
per line, first column; header/comment lines ignored). Shipped files:

    universe_sp500.csv           S&P 500
    universe_nasdaq100.csv       Nasdaq-100
    universe_liquid_largecap.csv large, liquid U.S.-listed names in neither
                                 index (mostly ADRs and big recent listings)

Dropping another CSV in `config/` extends the universe with no code change.
Overlap between files is expected — they are unioned into a set.

Expanding the universe is deliberately cheap: every stage that touches all of
it (screening, discovery, ranking) is pure Python over data already in the
database, so a wider net costs CPU and nothing else. The quality bar is
enforced downstream by the screener's price / liquidity / market-cap /
data-coverage filters and by the risk engine — not by keeping the list short.

The Settings watchlist still exists but only as "highlighted tickers" for the
UI and briefs; it never limits which symbols the screener, analysts, strategy
selector, or risk engine may operate on. Any universe ticker (or an explicit
on-demand ticker, e.g. chat "Should I buy XYZ?") can produce a BUY/SELL/NO
TRADE signal, and every signal still passes through the pure-Python risk gate.
"""

from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from sentinel.config import PROJECT_ROOT

MARKET_SYMBOLS = ["SPY"]  # always ingested; regime detection needs it

UNIVERSE_DIR = PROJECT_ROOT / "config"
UNIVERSE_GLOB = "universe_*.csv"
UNIVERSE_CSV = UNIVERSE_DIR / "universe_sp500.csv"  # retained for reference


def universe_files() -> tuple[Path, ...]:
    """Every universe CSV currently present, in stable order."""
    return tuple(sorted(UNIVERSE_DIR.glob(UNIVERSE_GLOB)))


def _read_symbols(path: Path) -> set[str]:
    symbols: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            sym = line.split(",")[0].strip().upper()
            if not sym or sym == "SYMBOL" or sym.startswith("#"):
                continue
            symbols.add(sym)
    return symbols


@lru_cache
def load_static_universe(path: Path | None = None) -> tuple[str, ...]:
    """Tickers from the universe CSVs (or a single explicit file for tests)."""
    if path is not None:
        return tuple(sorted(_read_symbols(path)))
    symbols: set[str] = set()
    for csv_path in universe_files():
        symbols |= _read_symbols(csv_path)
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
