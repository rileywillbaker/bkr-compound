"""News-triggered discovery: builds the dynamic candidate list that decides
which universe tickers receive full (LLM) pipeline analysis.

Everything here is deterministic Python over data already ingested into the
DB (news, earnings calendar, insider transactions, SEC filings, bars), so
sweeping the full ~500-name universe costs zero LLM tokens. Full analysis
then runs only on: candidates + highlighted watchlist + held positions.
That is how a mover like RGTI gets discovered without paying to analyze 500
names three times a day.

Triggers (each yields a scored DiscoveryEvent):
  earnings_surprise : actual EPS beat/missed estimate by >= threshold, recent
  high_impact_news  : material-event keyword hit or a 24h news-volume spike
  insider_cluster   : >= N distinct insiders net-buying in the lookback window
  unusual_volume    : last daily volume >= ratio x trailing 20-day average
  macro_move        : 1-day move with z-score >= threshold vs its own history
  fresh_filing      : 8-K filed within the last 2 days

The latest result is persisted to app_settings (consumed by scan symbol
selection) and to system_events (audit trail). The risk engine is untouched:
every signal for every ticker still terminates in the pure-Python risk gate.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import mean, pstdev
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.data.universe import MARKET_SYMBOLS, get_universe, held_symbols
from sentinel.db.models import (
    BarRow,
    EarningsCalendarRow,
    FilingRow,
    InsiderTransactionRow,
    NewsItemRow,
    SystemEvent,
)

log = structlog.get_logger()

DISCOVERY_KEY = "discovery_candidates"  # app_settings key holding the latest run

# Material-event phrases matched (lowercased) against headlines. Deterministic
# by design — no LLM is spent building the candidate list.
HIGH_IMPACT_KEYWORDS: tuple[str, ...] = (
    "acquisition",
    "acquire",
    "merger",
    "buyout",
    "takeover",
    "tender offer",
    "fda approval",
    "fda clear",
    "breakthrough",
    "upgrade",
    "downgrade",
    "raises guidance",
    "cuts guidance",
    "raises outlook",
    "cuts outlook",
    "profit warning",
    "earnings beat",
    "earnings miss",
    "bankruptcy",
    "chapter 11",
    "investigation",
    "sec probe",
    "lawsuit",
    "recall",
    "contract award",
    "wins contract",
    "activist",
    "short seller",
    "trading halted",
    "spin-off",
    "spinoff",
)

EventKind = Literal[
    "earnings_surprise",
    "high_impact_news",
    "insider_cluster",
    "unusual_volume",
    "macro_move",
    "fresh_filing",
]


class DiscoveryParams(BaseModel):
    """Thresholds for the deterministic triggers (tunable, no code changes)."""

    earnings_lookback_days: int = Field(default=3, ge=1)
    earnings_surprise_min_pct: float = Field(default=5.0, ge=0)
    news_window_hours: int = Field(default=24, ge=1)
    news_spike_min_items: int = Field(default=5, ge=1)
    insider_lookback_days: int = Field(default=14, ge=1)
    insider_cluster_min_buyers: int = Field(default=3, ge=1)
    volume_ratio_min: float = Field(default=2.5, ge=1)
    move_zscore_min: float = Field(default=2.5, ge=0)
    filing_forms: list[str] = Field(default_factory=lambda: ["8-K"])
    filing_lookback_days: int = Field(default=2, ge=1)
    max_candidates: int = Field(default=25, ge=1)  # hard cap = LLM cost cap


class DiscoveryEvent(BaseModel):
    symbol: str
    kind: EventKind
    detail: str
    score: float = Field(ge=0)


class DiscoveryResult(BaseModel):
    as_of: datetime
    universe_size: int
    events: list[DiscoveryEvent] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)  # ranked, capped


# ----------------------------------------------------------- triggers ----
def _earnings_surprises(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    since = date.today() - timedelta(days=p.earnings_lookback_days)
    rows = db.execute(
        select(EarningsCalendarRow).where(
            EarningsCalendarRow.date >= since,
            EarningsCalendarRow.date <= date.today(),
            EarningsCalendarRow.eps_actual.is_not(None),
            EarningsCalendarRow.eps_estimate.is_not(None),
        )
    ).scalars().all()
    events = []
    for r in rows:
        if r.symbol not in universe or r.eps_actual is None or not r.eps_estimate:
            continue
        surprise_pct = (r.eps_actual - r.eps_estimate) / abs(r.eps_estimate) * 100
        if abs(surprise_pct) >= p.earnings_surprise_min_pct:
            events.append(
                DiscoveryEvent(
                    symbol=r.symbol,
                    kind="earnings_surprise",
                    detail=(
                        f"EPS {r.eps_actual:.2f} vs est {r.eps_estimate:.2f} "
                        f"({surprise_pct:+.1f}%) on {r.date}"
                    ),
                    score=min(3.0, 1.0 + abs(surprise_pct) / 20),
                )
            )
    return events


def _high_impact_news(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    since = datetime.now(UTC) - timedelta(hours=p.news_window_hours)
    rows = db.execute(
        select(NewsItemRow.symbol, NewsItemRow.headline).where(
            NewsItemRow.symbol.is_not(None), NewsItemRow.published_at >= since
        )
    ).all()
    counts: dict[str, int] = defaultdict(int)
    keyword_hits: dict[str, str] = {}
    for symbol, headline in rows:
        if symbol not in universe:
            continue
        counts[symbol] += 1
        lowered = (headline or "").lower()
        if symbol not in keyword_hits:
            for kw in HIGH_IMPACT_KEYWORDS:
                if kw in lowered:
                    keyword_hits[symbol] = headline
                    break
    events = []
    for symbol in set(counts) | set(keyword_hits):
        spike = counts[symbol] >= p.news_spike_min_items
        hit = keyword_hits.get(symbol)
        if not (spike or hit):
            continue
        detail = f"{counts[symbol]} headlines/{p.news_window_hours}h"
        if hit:
            detail += f'; keyword hit: "{hit[:120]}"'
        events.append(
            DiscoveryEvent(
                symbol=symbol,
                kind="high_impact_news",
                detail=detail,
                score=(1.5 if hit else 0.0) + (1.0 if spike else 0.0),
            )
        )
    return events


def _insider_clusters(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    since = date.today() - timedelta(days=p.insider_lookback_days)
    rows = db.execute(
        select(InsiderTransactionRow).where(InsiderTransactionRow.transaction_date >= since)
    ).scalars().all()
    buyers: dict[str, set[str]] = defaultdict(set)
    net: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.symbol not in universe:
            continue
        net[r.symbol] += r.share_change
        if r.share_change > 0:
            buyers[r.symbol].add(r.name)
    events = []
    for symbol, names in buyers.items():
        if len(names) >= p.insider_cluster_min_buyers and net[symbol] > 0:
            events.append(
                DiscoveryEvent(
                    symbol=symbol,
                    kind="insider_cluster",
                    detail=(
                        f"{len(names)} distinct insiders net-buying "
                        f"{net[symbol]:,} shares over {p.insider_lookback_days}d"
                    ),
                    score=2.0,
                )
            )
    return events


def _volume_and_moves(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """Unusual volume + outsized (macro-sensitive) 1-day moves, from daily
    bars already in the DB — one query for the whole universe."""
    cutoff = datetime.now(UTC) - timedelta(days=45)
    rows = db.execute(
        select(BarRow.symbol, BarRow.ts, BarRow.close, BarRow.volume)
        .where(BarRow.timeframe == "1Day", BarRow.ts >= cutoff)
        .order_by(BarRow.symbol, BarRow.ts)
    ).all()
    series: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for symbol, _ts, close, volume in rows:
        if symbol in universe:
            series[symbol].append((float(close), int(volume)))

    events = []
    for symbol, points in series.items():
        if len(points) < 21:
            continue
        closes = [c for c, _ in points]
        volumes = [v for _, v in points]
        avg_vol = mean(volumes[-21:-1])
        if avg_vol > 0 and volumes[-1] >= p.volume_ratio_min * avg_vol:
            ratio = volumes[-1] / avg_vol
            events.append(
                DiscoveryEvent(
                    symbol=symbol,
                    kind="unusual_volume",
                    detail=f"volume {ratio:.1f}x the 20-day average",
                    score=min(2.5, ratio / 2),
                )
            )
        returns = [
            closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]
        ]
        if len(returns) >= 21:
            hist, last = returns[:-1][-20:], returns[-1]
            sigma = pstdev(hist)
            if sigma > 0:
                z = (last - mean(hist)) / sigma
                if abs(z) >= p.move_zscore_min:
                    events.append(
                        DiscoveryEvent(
                            symbol=symbol,
                            kind="macro_move",
                            detail=f"1-day move {last:+.1%} (z-score {z:+.1f})",
                            score=min(2.5, abs(z) / 2),
                        )
                    )
    return events


def _fresh_filings(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    since = date.today() - timedelta(days=p.filing_lookback_days)
    rows = db.execute(
        select(FilingRow).where(
            FilingRow.form.in_(p.filing_forms), FilingRow.filed_at >= since
        )
    ).scalars().all()
    seen: set[str] = set()
    events = []
    for r in rows:
        if r.symbol not in universe or r.symbol in seen:
            continue
        seen.add(r.symbol)
        events.append(
            DiscoveryEvent(
                symbol=r.symbol,
                kind="fresh_filing",
                detail=f"{r.form} filed {r.filed_at}: {r.description[:100]}",
                score=1.0,
            )
        )
    return events


# ------------------------------------------------------------- driver ----
def discover(db: Session, params: DiscoveryParams | None = None) -> DiscoveryResult:
    """Sweep the universe, rank symbols by summed event score, persist the
    capped candidate list for the day's scans."""
    from sentinel.db.settings_store import set_setting

    p = params or DiscoveryParams()
    universe = set(get_universe(db)) - set(MARKET_SYMBOLS)
    events: list[DiscoveryEvent] = []
    for trigger in (
        _earnings_surprises,
        _high_impact_news,
        _insider_clusters,
        _volume_and_moves,
        _fresh_filings,
    ):
        try:
            events.extend(trigger(db, universe, p))
        except Exception:
            log.exception("discovery trigger failed", trigger=trigger.__name__)

    totals: dict[str, float] = defaultdict(float)
    for e in events:
        totals[e.symbol] += e.score
    ranked = sorted(totals, key=lambda s: (-totals[s], s))
    candidates = ranked[: p.max_candidates]

    result = DiscoveryResult(
        as_of=datetime.now(UTC),
        universe_size=len(universe),
        events=sorted(events, key=lambda e: (-e.score, e.symbol)),
        candidates=candidates,
    )
    set_setting(
        db,
        DISCOVERY_KEY,
        {
            "as_of": result.as_of.isoformat(),
            "candidates": candidates,
            "events": [e.model_dump() for e in result.events],
        },
    )
    db.add(
        SystemEvent(
            kind="discovery.run",
            message=(
                f"discovery over {result.universe_size} universe tickers: "
                f"{len(events)} events, {len(candidates)} candidates"
            ),
            payload={"candidates": candidates},
        )
    )
    db.flush()
    log.info("discovery complete", events=len(events), candidates=candidates)
    return result


def get_candidates(db: Session, max_age_hours: int = 30) -> list[str]:
    """Latest persisted candidate list; empty when missing or stale (so a
    dead worker can't pin an outdated list forever)."""
    from sentinel.db.settings_store import get_setting

    stored = get_setting(db, DISCOVERY_KEY)
    if not isinstance(stored, dict):
        return []
    try:
        as_of = datetime.fromisoformat(stored["as_of"])
        if datetime.now(UTC) - as_of > timedelta(hours=max_age_hours):
            return []
        return [str(s).upper() for s in stored.get("candidates", [])]
    except (KeyError, ValueError, TypeError):
        return []


def get_scan_symbols(db: Session) -> list[str]:
    """What a scheduled scan actually analyzes with the LLM: discovery
    candidates + highlighted watchlist + held positions. The watchlist no
    longer limits anything — it only guarantees its names are always scanned."""
    from sentinel.db.settings_store import get_watchlist

    return sorted(set(get_candidates(db)) | set(get_watchlist(db)) | set(held_symbols(db)))


def insider_net_shares(db: Session, symbol: str, days: int = 90) -> int | None:
    """Net insider share change over the window; None when no filings are
    ingested (analysts mark the factor unavailable rather than assume 0)."""
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        select(InsiderTransactionRow.share_change).where(
            InsiderTransactionRow.symbol == symbol,
            InsiderTransactionRow.transaction_date >= since,
        )
    ).all()
    if not rows:
        return None
    return int(sum(change for (change,) in rows))
