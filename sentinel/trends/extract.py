"""Deterministic extraction: free text → theme keys and ticker symbols.

No LLM. Every document collected from every free source passes through here,
which is exactly the kind of over-the-whole-corpus stage that must never cost
a token (CLAUDE.md's cost rules). It is plain regex and set membership.

Two extraction problems, handled differently:

* **Themes** — keyword match on word boundaries against the taxonomy. Cheap
  and precise, because the taxonomy's keywords are domain phrases ("small
  modular reactor") rather than bare words.

* **Tickers** — genuinely ambiguous in prose. A naive "any 1-5 upper-case
  run is a ticker" rule turns CEO, USA, GDP, ETF and every sentence-initial
  word into a holding. The rules below are deliberately conservative:
  cashtags always count; bare upper-case tokens only count if they are a
  known symbol AND not a common English/finance abbreviation; company names
  count only when long and distinctive enough to be unambiguous.

Precision beats recall here. A missed ticker costs one candidate; a false one
puts a garbage name in front of the user with a dollar amount attached.
"""

import re
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.trends.taxonomy import THEMES

# Upper-case tokens that are never a ticker in financial prose, even though
# several are also real listed symbols (T, ALL, KEY, NOW, CEO-adjacent noise).
# Requiring a cashtag for these is the safe default.
_STOPWORD_TICKERS: frozenset[str] = frozenset({
    "A", "I", "AI", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN",
    "IS", "IT", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US",
    "WE", "AM", "PM", "ET", "PT", "UTC", "EST", "EDT",
    "CEO", "CFO", "COO", "CTO", "CIO", "EPS", "IPO", "ETF", "GDP", "CPI",
    "PPI", "FED", "FOMC", "SEC", "FDA", "FTC", "DOJ", "IRS", "USA", "USD",
    "EUR", "GBP", "NYSE", "OTC", "AMEX", "SPAC", "REIT", "ESG", "AGM",
    "Q1", "Q2", "Q3", "Q4", "FY", "YOY", "QOQ", "TTM", "YTD", "EBITDA",
    "ROI", "ROE", "ROIC", "PE", "PEG", "EV", "FCF", "CAGR", "IPOS",
    "AND", "THE", "FOR", "NOT", "BUT", "ALL", "NEW", "NOW", "OUT", "OWN",
    "TOP", "BIG", "LOW", "HIGH", "BUY", "SELL", "HOLD", "LONG", "PUT",
    "CALL", "PUTS", "CALLS", "BULL", "BEAR", "RISK", "CASH", "DEBT",
    "KEY", "ONE", "TWO", "SEE", "GET", "HAS", "HAD", "WAS", "ARE", "CAN",
    "MAY", "END", "OLD", "WELL", "BEST", "GOOD", "REAL", "OPEN", "NEXT",
    "FAST", "FREE", "SAFE", "PLAY", "MOVE", "PLAN", "WORK", "LIFE", "CARE",
    "MAIN", "EDIT", "POST", "LINK", "SITE", "PAGE", "LOVE", "HOPE", "WISH",
    "NEWS", "DATA", "TECH", "AWS", "API", "SAAS", "IOT", "EVS",
    "JPY", "CNY", "OPEC", "NATO", "EU", "UK", "UN", "DOD", "DOE",
    "NRC", "EPA", "FERC", "NASA", "GAO", "OMB", "CBO", "WSJ", "CNBC",
    "PDF", "URL", "HTTP", "HTTPS", "WWW", "LLC", "INC", "LTD", "CORP",
    "PLC", "CO", "SA", "NV", "AG", "SMR", "HALEU", "GW", "MW", "TWH",
    "DD", "YOLO", "ATH", "ATL", "FUD", "IMO", "TLDR", "EOD", "EOW",
})

# Company-name suffixes stripped before name matching.
_CORP_SUFFIX_RE = re.compile(
    r"\b(incorporated|corporation|company|holdings?|group|limited|"
    r"technologies|technology|industries|international|enterprises|"
    r"resources|energy|systems|solutions|partners|plc|inc|corp|co|ltd|"
    r"llc|sa|nv|ag|se|the)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE_RE = re.compile(r"\s+")

_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z.\-]{0,5})\b")
_UPPER_TOKEN_RE = re.compile(r"\b([A-Z][A-Z.\-]{0,5})\b")

# A company name must survive normalisation at this length to be matched in
# prose. Five characters keeps genuinely distinctive names ("Cameco",
# "Nvidia", "Palantir") while dropping the short ones that would sweep up
# unrelated sentences ("Now", "Aon", "Vale").
_MIN_NAME_LENGTH = 5

# Normalised names that are also ordinary English and would match constantly.
# Length alone cannot catch these — "Energy Transfer" normalises to "transfer".
_GENERIC_NAMES: frozenset[str] = frozenset({
    "transfer", "capital", "growth", "value", "income", "digital", "global",
    "american", "national", "united", "general", "standard", "atlantic",
    "pacific", "northern", "southern", "eastern", "western", "central",
    "first", "premier", "select", "advance", "advanced", "applied",
    "materials", "sciences", "service", "services", "products", "brands",
    "communications", "media", "networks", "software", "semiconductor",
    "electric", "power", "water", "steel", "metals", "mining", "minerals",
    "trust", "realty", "properties", "bancorp", "financial", "insurance",
    "health", "healthcare", "medical", "pharma", "biosciences", "therapeutics",
})


@lru_cache(maxsize=1)
def _theme_patterns() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    """Compiled keyword patterns per theme, built once per process."""
    out = []
    for theme in THEMES:
        patterns = tuple(
            re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
            for kw in theme.keywords
        )
        out.append((theme.key, patterns))
    return tuple(out)


def extract_themes(text: str) -> list[str]:
    """Theme keys this document is about. Empty when it is about none of them."""
    if not text:
        return []
    return [key for key, patterns in _theme_patterns() if any(p.search(text) for p in patterns)]


def theme_keyword_hits(text: str) -> dict[str, list[str]]:
    """Theme key → the specific keywords that matched, for the evidence trail.

    The report has to be able to say *why* a document counted toward a theme;
    an opaque boolean would make the strength score unauditable.
    """
    if not text:
        return {}
    hits: dict[str, list[str]] = {}
    for theme in THEMES:
        matched = [
            kw for kw in theme.keywords
            if re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE)
        ]
        if matched:
            hits[theme.key] = matched
    return hits


def _normalise_name(name: str) -> str:
    cleaned = _CORP_SUFFIX_RE.sub(" ", name.lower())
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def build_name_index(db: Session) -> dict[str, str]:
    """Distinctive company name → symbol, from fundamentals already stored.

    Only names that survive normalisation at >= _MIN_NAME_LENGTH characters
    are included, and a name mapping to more than one symbol is dropped
    entirely rather than guessed at.
    """
    from sentinel.db.models import FundamentalsRow

    rows = db.execute(
        select(FundamentalsRow.symbol, FundamentalsRow.name).where(FundamentalsRow.name != "")
    ).all()
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for symbol, name in rows:
        key = _normalise_name(name or "")
        if len(key) < _MIN_NAME_LENGTH or key in _GENERIC_NAMES:
            continue
        existing = index.get(key)
        if existing and existing != symbol:
            ambiguous.add(key)
        else:
            index[key] = symbol
    for key in ambiguous:
        index.pop(key, None)
    return index


def extract_symbols(
    text: str,
    known: frozenset[str] | set[str],
    name_index: dict[str, str] | None = None,
) -> list[str]:
    """Tickers mentioned in `text`, restricted to `known`.

    `known` is the caller's allow-list (the trading universe ∪ taxonomy seeds
    ∪ tracked ETFs). Nothing outside it is ever returned, which is what keeps
    hallucinated or foreign-listed symbols out of the pipeline.
    """
    if not text:
        return []
    found: set[str] = set()
    known_upper = {s.upper() for s in known}

    # Cashtags are unambiguous by construction — a human explicitly marked it.
    for match in _CASHTAG_RE.finditer(text):
        symbol = match.group(1).upper().rstrip(".")
        if symbol in known_upper:
            found.add(symbol)

    # Bare upper-case tokens: known symbol, and not a common abbreviation.
    for match in _UPPER_TOKEN_RE.finditer(text):
        symbol = match.group(1).upper().rstrip(".")
        if symbol in _STOPWORD_TICKERS or len(symbol) < 2:
            continue
        if symbol in known_upper:
            found.add(symbol)

    # Company names, normalised on both sides.
    if name_index:
        haystack = " " + _WHITESPACE_RE.sub(" ", _NON_ALNUM_RE.sub(" ", text.lower())) + " "
        for name, symbol in name_index.items():
            if symbol.upper() in known_upper and f" {name} " in haystack:
                found.add(symbol.upper())

    return sorted(found)


@lru_cache(maxsize=1)
def stopword_tickers() -> frozenset[str]:
    """Exposed for tests and for the API's extraction-diagnostics endpoint."""
    return _STOPWORD_TICKERS
