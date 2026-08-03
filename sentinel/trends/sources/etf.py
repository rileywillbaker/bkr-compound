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
they are fetched at a polite pace, once a day. iShares product identifiers are
part of the public URL scheme and are marked `verified=False` where they have
not been confirmed against a live response on this machine — an unverified
endpoint that 404s simply contributes nothing.
"""

from __future__ import annotations

import csv
import io
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
    etf: str
    url: str
    issuer: str
    verified: bool = False


# ARK publishes clean daily CSVs at stable, documented URLs — the most
# reliable free holdings source in the industry.
_ARK = "https://assets.ark-funds.com/fund-documents/funds-etf-csv"

# Global X exposes a full-holdings CSV download on each fund page.
_GLOBALX = "https://www.globalxetfs.com/funds/{slug}/?download_full_holdings=true"

# iShares' public holdings download. The numeric segment is the fund's product
# id in iShares' own URL scheme.
_ISHARES = (
    "https://www.ishares.com/us/products/{product_id}/fund/1467271812596.ajax"
    "?fileType=csv&fileName={etf}_holdings&dataType=fund"
)

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
    HoldingsEndpoint(etf="URA", issuer="globalx", url=_GLOBALX.format(slug="ura")),
    HoldingsEndpoint(etf="LIT", issuer="globalx", url=_GLOBALX.format(slug="lit")),
    HoldingsEndpoint(etf="BOTZ", issuer="globalx", url=_GLOBALX.format(slug="botz")),
    HoldingsEndpoint(etf="BUG", issuer="globalx", url=_GLOBALX.format(slug="bug")),
    HoldingsEndpoint(etf="AIQ", issuer="globalx", url=_GLOBALX.format(slug="aiq")),
    HoldingsEndpoint(
        etf="ITA", issuer="ishares", url=_ISHARES.format(product_id="239502", etf="ITA")
    ),
    HoldingsEndpoint(
        etf="ICLN", issuer="ishares", url=_ISHARES.format(product_id="239738", etf="ICLN")
    ),
    HoldingsEndpoint(
        etf="SOXX", issuer="ishares", url=_ISHARES.format(product_id="239705", etf="SOXX")
    ),
    HoldingsEndpoint(
        etf="IHAK", issuer="ishares", url=_ISHARES.format(product_id="307352", etf="IHAK")
    ),
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


def fetch_holdings(endpoint: HoldingsEndpoint) -> list[HoldingRecord]:
    """Download and parse one ETF's holdings. Empty list on any failure."""
    body = fetch(endpoint.url, provider="etf_issuer")
    if body is None:
        return []
    records = parse_holdings_csv(body, endpoint.etf)
    if not records:
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
