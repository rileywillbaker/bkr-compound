"""Thematic ETF activity from free data.

Two independent signals, because one of them is reliable and the other is
merely valuable:

**1. Flow proxy from our own bars (always available, zero external calls).**
Real creation/redemption data is a paid product. But an ETF's own price and
volume are ordinary market data we already ingest, and they carry most of the
same information: sustained above-average dollar volume alongside relative
strength versus SPY is what money moving into a theme looks like from the
outside. `config/thematic_etfs.csv` lists the ETFs whose bars get ingested for
exactly this purpose. This signal cannot break, because it depends on nothing
we do not already have.

**2. Published holdings (best effort).** Several issuers publish full daily
holdings free of charge. Where they do, snapshots are stored per date and
*diffed*, which is what answers the question actually asked — "what uranium
companies are ETFs increasing exposure to?" — as opposed to merely "what do
they hold?". Where an issuer does not publish, or changes its endpoint, the
theme falls back to its taxonomy constituents and the report says which basis
it used, rather than presenting a guess as a measurement.

The holdings endpoints below are public download links intended for investors;
they are fetched at a polite pace, once a day. Each was confirmed against a
live response on 2026-08-02 — see `verified`. An endpoint that stops working
simply contributes nothing, and the theme falls back to the bar-based proxy.

Each issuer publishes differently, so retrieval is per-issuer rather than one
URL template: ARK links a stable CSV directly, iShares serves
`latest-holdings.csv` under a product id, and Global X puts a DATED file on a
CDN whose name has to be read off the fund page. Three strategies, one parser.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date

import structlog
from pydantic import BaseModel

from sentinel.trends.sources.feeds import fetch
from sentinel.trends.taxonomy import thematic_etfs

log = structlog.get_logger()


class HoldingRecord(BaseModel):
    """One line of an ETF's published holdings file."""

    etf: str
    symbol: str
    name: str = ""
    weight_pct: float | None = None
    shares: float | None = None
    market_value: float | None = None


class HoldingsEndpoint(BaseModel):
    """How to reach one ETF's published holdings.

    Each issuer exposes them differently, so `issuer` selects the retrieval
    strategy rather than just labelling the row:

      ark      `url` is a direct, stable CSV link.
      ishares  `product_id` addresses /us/products/{id}/{slug}/latest-holdings.csv.
      globalx  the CSV lives on a CDN under a DATED filename, so the fund page
               is read first to discover the current link.
    """

    etf: str
    issuer: str
    url: str = ""  # ark only
    product_id: str = ""  # ishares only
    verified: bool = False


# ARK publishes clean daily CSVs at stable, documented URLs — the most
# reliable free holdings source in the industry.
_ARK = "https://assets.ark-funds.com/fund-documents/funds-etf-csv"

# Global X fund page. The page itself is not the CSV — it CONTAINS a link to
# one on the issuer's CDN whose filename embeds the holdings date, so the
# filename cannot be constructed and must be read from the page.
_GLOBALX_PAGE = "https://www.globalxetfs.com/funds/{slug}/"
_GLOBALX_CSV_RE = re.compile(
    r"https://assets\.globalxetfs\.com/funds/holdings/[a-z0-9\-]+_full-holdings_\d{8}\.csv",
    re.IGNORECASE,
)

# iShares' current public holdings download. Product ids come from iShares'
# own product screener and are stable; the {slug} segment is NOT used for
# routing (see _ISHARES_SLUGS).
_ISHARES = "https://www.ishares.com/us/products/{product_id}/{slug}/latest-holdings.csv"

# The slug is ignored when resolving the fund, but it IS part of the CDN cache
# key — and some cached objects are the header-only "empty" version of the
# file. Verified 2026-08-02: product 239502 (ITA) returns 52 holdings under
# slug "x" and an empty file under slug "fund", reproducibly, while ICLN is
# the other way round. Trying a couple of equivalent spellings of the same
# public document reaches a populated entry; it is not an attempt to evade
# anything, and each attempt stops at the first file that actually parses.
_ISHARES_SLUGS: tuple[str, ...] = ("fund", "x", "holdings")

HOLDINGS_ENDPOINTS: tuple[HoldingsEndpoint, ...] = (
    HoldingsEndpoint(
        etf="ARKQ",
        issuer="ark",
        verified=True,
        url=f"{_ARK}/ARK_AUTONOMOUS_TECH._%26_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    ),
    HoldingsEndpoint(
        etf="ARKX",
        issuer="ark",
        verified=True,
        url=f"{_ARK}/ARK_SPACE_EXPLORATION_%26_INNOVATION_ETF_ARKX_HOLDINGS.csv",
    ),
    # Global X — all verified live 2026-08-02.
    HoldingsEndpoint(etf="URA", issuer="globalx", verified=True),
    HoldingsEndpoint(etf="LIT", issuer="globalx", verified=True),
    HoldingsEndpoint(etf="BOTZ", issuer="globalx", verified=True),
    HoldingsEndpoint(etf="BUG", issuer="globalx", verified=True),
    HoldingsEndpoint(etf="AIQ", issuer="globalx", verified=True),
    HoldingsEndpoint(etf="COPX", issuer="globalx", verified=True),
    # iShares — product ids resolved from the issuer's own product screener
    # and confirmed live 2026-08-02. SOXX returns a header-only file under
    # every slug; that is upstream, and it simply contributes nothing.
    HoldingsEndpoint(etf="ITA", issuer="ishares", product_id="239502", verified=True),
    HoldingsEndpoint(etf="ICLN", issuer="ishares", product_id="239738", verified=True),
    HoldingsEndpoint(etf="IHAK", issuer="ishares", product_id="307352", verified=True),
    HoldingsEndpoint(etf="PICK", issuer="ishares", product_id="239655", verified=True),
    HoldingsEndpoint(etf="IFRA", issuer="ishares", product_id="294315", verified=True),
    HoldingsEndpoint(etf="SOXX", issuer="ishares", product_id="239705", verified=False),
)

# Column headers vary by issuer; these are the aliases seen in practice.
_SYMBOL_KEYS = ("ticker", "holding ticker", "symbol", "issue ticker")
_NAME_KEYS = ("company", "name", "security name", "holding name", "description")
_WEIGHT_KEYS = ("weight (%)", "weight", "% of net assets", "weighting", "portfolio weight")
_SHARES_KEYS = ("shares", "quantity", "shares held", "shares/par value", "sharesheld")
_VALUE_KEYS = ("market value ($)", "market value", "value", "market value (usd)")

_NUMERIC_STRIP = str.maketrans({",": "", "$": "", "%": "", "(": "-", ")": ""})


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = str(raw).strip().translate(_NUMERIC_STRIP)
    if not cleaned or cleaned in {"-", "--"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _pick(row: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        for actual, value in row.items():
            if actual and actual.strip().lower() == key:
                return value
    return None


def parse_holdings_csv(body: str, etf: str) -> list[HoldingRecord]:
    """Parse an issuer holdings CSV into records.

    Issuer files are messy: iShares prefixes several lines of fund metadata
    before the real header, and every issuer names its columns differently. The
    parser finds the header row by looking for a recognisable ticker column
    rather than assuming a fixed offset.
    """
    if not body or not body.strip():
        return []
    lines = body.splitlines()

    header_index = None
    for index, line in enumerate(lines[:40]):
        lowered = [cell.strip().lower() for cell in line.split(",")]
        if any(cell in _SYMBOL_KEYS for cell in lowered):
            header_index = index
            break
    if header_index is None:
        log.info("etf holdings: no recognisable header", etf=etf)
        return []

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    records: list[HoldingRecord] = []
    for row in reader:
        if not isinstance(row, dict):
            continue
        symbol = (_pick(row, _SYMBOL_KEYS) or "").strip().upper()
        # Cash, futures and FX lines have no usable ticker; skip rather than
        # inventing a holding.
        if not symbol or len(symbol) > 6 or not symbol.replace(".", "").isalnum():
            continue
        if symbol in {"CASH", "USD", "NA", "N/A", "--"}:
            continue
        records.append(
            HoldingRecord(
                etf=etf.upper(),
                symbol=symbol,
                name=(_pick(row, _NAME_KEYS) or "").strip()[:256],
                weight_pct=_to_float(_pick(row, _WEIGHT_KEYS)),
                shares=_to_float(_pick(row, _SHARES_KEYS)),
                market_value=_to_float(_pick(row, _VALUE_KEYS)),
            )
        )
    return records


def _fetch_direct(endpoint: HoldingsEndpoint) -> list[HoldingRecord]:
    """ARK: a stable, direct CSV link."""
    body = fetch(endpoint.url, provider="etf_issuer")
    return parse_holdings_csv(body, endpoint.etf) if body else []


def _fetch_ishares(endpoint: HoldingsEndpoint) -> list[HoldingRecord]:
    """iShares: latest-holdings.csv, retried across equivalent slugs.

    Stops at the first response that actually contains holdings, so the common
    case is a single request. See _ISHARES_SLUGS for why more than one URL
    spelling is needed.
    """
    for slug in _ISHARES_SLUGS:
        body = fetch(
            _ISHARES.format(product_id=endpoint.product_id, slug=slug),
            provider="etf_issuer",
        )
        if not body:
            continue
        records = parse_holdings_csv(body, endpoint.etf)
        if records:
            return records
    return []


def _fetch_globalx(endpoint: HoldingsEndpoint) -> list[HoldingRecord]:
    """Global X: read the fund page, then follow the dated CDN link it names."""
    page = fetch(
        _GLOBALX_PAGE.format(slug=endpoint.etf.lower()), provider="etf_issuer"
    )
    if not page:
        return []
    match = _GLOBALX_CSV_RE.search(page)
    if match is None:
        log.info("globalx: no holdings link on fund page", etf=endpoint.etf)
        return []
    body = fetch(match.group(0), provider="etf_issuer")
    return parse_holdings_csv(body, endpoint.etf) if body else []


_FETCHERS = {
    "ark": _fetch_direct,
    "ishares": _fetch_ishares,
    "globalx": _fetch_globalx,
}


def fetch_holdings(endpoint: HoldingsEndpoint) -> list[HoldingRecord]:
    """Download and parse one ETF's holdings. Empty list on any failure."""
    fetcher = _FETCHERS.get(endpoint.issuer, _fetch_direct)
    try:
        records = fetcher(endpoint)
    except Exception:
        log.exception("etf holdings fetch failed", etf=endpoint.etf, issuer=endpoint.issuer)
        return []
    if not records:
        # Expected and harmless: an issuer serving an empty file today means
        # "no free holdings data", which scoring already reports honestly.
        log.info("etf holdings empty", etf=endpoint.etf, issuer=endpoint.issuer)
    return records


def collect_holdings(
    endpoints: tuple[HoldingsEndpoint, ...] = HOLDINGS_ENDPOINTS,
) -> tuple[dict[str, list[HoldingRecord]], list[str]]:
    """All published holdings we can get today, keyed by ETF."""
    out: dict[str, list[HoldingRecord]] = {}
    answered: list[str] = []
    for endpoint in endpoints:
        records = fetch_holdings(endpoint)
        if records:
            out[endpoint.etf] = records
            answered.append(f"etf_holdings:{endpoint.etf}")
    log.info("etf holdings collected", etfs=len(out))
    return out, answered


def tracked_etfs() -> list[str]:
    """Every ETF whose bars the flow proxy needs."""
    return thematic_etfs()


def store_holdings(db, holdings: dict[str, list[HoldingRecord]], as_of: date | None = None) -> int:
    """Persist one day's snapshots. Re-running the same day overwrites, so a
    retry never double-counts; a NEW day always creates new rows, which is
    what makes the day-over-day diff possible."""
    from sentinel.db.models import EtfHoldingRow

    day = as_of or date.today()
    written = 0
    for etf, records in holdings.items():
        for record in records:
            existing = db.get(EtfHoldingRow, (etf.upper(), record.symbol, day))
            if existing is None:
                db.add(
                    EtfHoldingRow(
                        etf=etf.upper(),
                        symbol=record.symbol,
                        as_of=day,
                        weight_pct=record.weight_pct,
                        shares=record.shares,
                        market_value=record.market_value,
                        name=record.name,
                    )
                )
            else:
                existing.weight_pct = record.weight_pct
                existing.shares = record.shares
                existing.market_value = record.market_value
                existing.name = record.name or existing.name
            written += 1
    db.flush()
    return written
