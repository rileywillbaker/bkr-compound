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
  earnings_revision : a streak of consecutive quarterly EPS beats — improving
                      earnings without needing a paid estimate-revision feed
  high_impact_news  : material-event keyword hit or a 24h news-volume spike
  insider_cluster   : >= N distinct insiders net-buying in the lookback window
  unusual_volume    : last daily volume >= ratio x trailing 20-day average
  macro_move        : 1-day move with z-score >= threshold vs its own history
  fresh_filing      : 8-K filed within the last 2 days
  relative_strength : outperforming SPY over the last quarter while still in
                      its own uptrend — the single most durable free screen
  breakout          : pushing into 52-week-high territory on real volume
  uptrend_pullback  : dip below the 20-day average while the 200-day trend is
                      still intact — buying strength on sale, not catching a
                      falling knife
  sector_leadership : belongs to one of the strongest sectors AND leads within
                      it
  pullback_from_high: trading well below its 52-week high — the gradual,
                      multi-week drawdown that the single-day triggers above
                      (unusual_volume, macro_move) structurally cannot catch,
                      since nothing about it is unusual on any *one* day
  elevated_short_interest : short interest elevated enough to flag for
                      review (data permitting — see fintel provider)
  finviz_screen     : Finviz Elite screen for a deep pullback, run across the
                      whole market in one call — the only trigger that can
                      surface a symbol outside the static universe entirely

A quality gate then drops anything the data shows to be a penny stock or too
thin to trade before the list is capped. It fails OPEN on missing data — the
screener and the risk engine both fail CLOSED later, so a name with no bars
yet still gets looked at rather than silently disappearing.

The latest result is persisted to app_settings (consumed by scan symbol
selection and by the LLM-gating stage as the "material event" source) and to
system_events (audit trail). The risk engine is untouched: every signal for
every ticker still terminates in the pure-Python risk gate.
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
    FundamentalsRow,
    InsiderTransactionRow,
    NewsItemRow,
    ShortInterestRow,
    SystemEvent,
)
from sentinel.providers.base import ProviderError
from sentinel.providers.registry import CredentialsMissing, build_screener

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
    "earnings_revision",
    "high_impact_news",
    "insider_cluster",
    "unusual_volume",
    "macro_move",
    "fresh_filing",
    "relative_strength",
    "breakout",
    "uptrend_pullback",
    "sector_leadership",
    "pullback_from_high",
    "elevated_short_interest",
    "finviz_screen",
]


class DiscoveryParams(BaseModel):
    """Thresholds for the deterministic triggers (tunable, no code changes)."""

    earnings_lookback_days: int = Field(default=3, ge=1)
    earnings_surprise_min_pct: float = Field(default=5.0, ge=0)
    earnings_beat_streak: int = Field(default=2, ge=1)  # consecutive quarterly beats
    news_window_hours: int = Field(default=24, ge=1)
    news_spike_min_items: int = Field(default=5, ge=1)
    insider_lookback_days: int = Field(default=14, ge=1)
    insider_cluster_min_buyers: int = Field(default=3, ge=1)
    volume_ratio_min: float = Field(default=2.5, ge=1)
    move_zscore_min: float = Field(default=2.5, ge=0)
    filing_forms: list[str] = Field(default_factory=lambda: ["8-K"])
    filing_lookback_days: int = Field(default=2, ge=1)
    pullback_min_pct: float = Field(default=15.0, ge=0)  # min % below 52-week high
    pullback_max_pct: float = Field(
        default=45.0, ge=0
    )  # above this it's more likely a broken business than a dip
    short_interest_min_pct: float = Field(default=15.0, ge=0)  # % of float, short
    finviz_pullback_min_pct: int = Field(default=30, ge=0)  # Finviz screen threshold

    # --- momentum / trend family (all computed from stored bars) ---
    series_lookback_days: int = Field(default=400, ge=60)
    rs_lookback: int = Field(default=63, ge=5)  # ~one quarter of trading days
    rs_min_excess_pct: float = Field(default=10.0, ge=0)  # vs the benchmark
    breakout_max_pct_below_high: float = Field(default=2.0, ge=0)
    breakout_min_rel_volume: float = Field(default=1.3, ge=0)
    uptrend_pullback_min_pct: float = Field(default=5.0, ge=0)
    uptrend_pullback_max_pct: float = Field(default=20.0, ge=0)
    sector_top_n: int = Field(default=3, ge=1)

    # --- quality gate (applied to the final list; fails OPEN on missing data) ---
    min_price: float = Field(default=5.0, ge=0)  # no penny stocks
    min_avg_dollar_volume: float = Field(default=5_000_000, ge=0)  # no illiquid names

    max_candidates: int = Field(default=25, ge=1)  # hard cap = downstream work cap


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


Series = dict[str, list[tuple[float, int]]]  # symbol -> [(close, volume), ...] ascending


def _daily_series(db: Session, symbols: set[str], days: int) -> Series:
    """One query for the whole universe's daily bars.

    Every technical trigger below reads from this single result set, so
    sweeping 700 names costs one round trip and some arithmetic — which is the
    entire reason the universe can be large without the bill moving.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(
        select(BarRow.symbol, BarRow.ts, BarRow.close, BarRow.volume)
        .where(BarRow.timeframe == "1Day", BarRow.ts >= cutoff)
        .order_by(BarRow.symbol, BarRow.ts)
    ).all()
    series: Series = defaultdict(list)
    for symbol, _ts, close, volume in rows:
        if symbol in symbols:
            series[symbol].append((float(close), int(volume)))
    return series


def _pct_return(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback or closes[-1 - lookback] <= 0:
        return None
    return (closes[-1] / closes[-1 - lookback] - 1) * 100


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _volume_and_moves(
    series: Series, p: DiscoveryParams
) -> tuple[list[DiscoveryEvent], dict[str, float]]:
    """Unusual volume + outsized (macro-sensitive) 1-day moves, from daily
    bars already in the DB.

    Also returns a continuous per-symbol "activity" measure (volume ratio +
    |move z-score|, even below the event thresholds) used as the ranking
    tie-breaker in discover(). Without it, a batch of same-score events —
    e.g. dozens of routine 8-K filings at a flat 1.0 — ties, and the
    max_candidates cap degenerates into an alphabetical slice of the
    universe (observed: candidates ABT..DLTR, all A-D names)."""
    events = []
    activity: dict[str, float] = {}
    for symbol, points in series.items():
        if len(points) < 21:
            continue
        closes = [c for c, _ in points]
        volumes = [v for _, v in points]
        avg_vol = mean(volumes[-21:-1])
        if avg_vol > 0:
            ratio = volumes[-1] / avg_vol
            activity[symbol] = activity.get(symbol, 0.0) + min(10.0, ratio)
            if volumes[-1] >= p.volume_ratio_min * avg_vol:
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
                activity[symbol] = activity.get(symbol, 0.0) + min(10.0, abs(z))
                if abs(z) >= p.move_zscore_min:
                    events.append(
                        DiscoveryEvent(
                            symbol=symbol,
                            kind="macro_move",
                            detail=f"1-day move {last:+.1%} (z-score {z:+.1f})",
                            score=min(2.5, abs(z) / 2),
                        )
                    )
    return events, activity


def _technical_setups(
    series: Series, benchmark: list[float], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """Relative strength, breakouts, and pullbacks inside an intact uptrend.

    These are the classic momentum screens, and they are exactly the kind of
    thing that should never cost a token: everything below is arithmetic over
    bars already stored. Each requires enough history to be meaningful, so
    thinly-covered symbols simply produce no event.
    """
    bench_return = _pct_return(benchmark, p.rs_lookback)
    events: list[DiscoveryEvent] = []
    for symbol, points in series.items():
        closes = [c for c, _ in points]
        volumes = [v for _, v in points]
        last = closes[-1] if closes else 0.0
        if last <= 0:
            continue

        sma50 = _sma(closes, 50)
        sma20 = _sma(closes, 20)
        sma200 = _sma(closes, 200)
        window = closes[-252:]
        high = max(window) if window else 0.0
        pct_below_high = (high - last) / high * 100 if high > 0 else None

        # --- relative strength vs the benchmark, trend still intact ---------
        own_return = _pct_return(closes, p.rs_lookback)
        if (
            bench_return is not None
            and own_return is not None
            and sma50 is not None
            and last > sma50
        ):
            excess = own_return - bench_return
            if excess >= p.rs_min_excess_pct:
                events.append(
                    DiscoveryEvent(
                        symbol=symbol,
                        kind="relative_strength",
                        detail=(
                            f"outperformed the benchmark by {excess:+.0f} points over "
                            f"{p.rs_lookback} sessions, still above its 50-day average"
                        ),
                        score=min(2.5, 1.0 + excess / 40),
                    )
                )

        # --- breakout: 52-week-high territory ON VOLUME ---------------------
        # The volume requirement matters: a quiet drift to a new high is not a
        # breakout, and without it every steadily-rising name would qualify.
        if pct_below_high is not None and len(volumes) >= 21:
            avg_vol = mean(volumes[-21:-1])
            rel_vol = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
            if (
                pct_below_high <= p.breakout_max_pct_below_high
                and rel_vol >= p.breakout_min_rel_volume
            ):
                events.append(
                    DiscoveryEvent(
                        symbol=symbol,
                        kind="breakout",
                        detail=(
                            f"within {pct_below_high:.1f}% of its 52-week high on "
                            f"{rel_vol:.1f}x average volume"
                        ),
                        score=min(2.5, 1.2 + rel_vol / 4),
                    )
                )

        # --- pullback inside an intact uptrend ------------------------------
        if (
            sma20 is not None
            and sma200 is not None
            and pct_below_high is not None
            and last < sma20
            and last > sma200
            and p.uptrend_pullback_min_pct <= pct_below_high <= p.uptrend_pullback_max_pct
        ):
            events.append(
                DiscoveryEvent(
                    symbol=symbol,
                    kind="uptrend_pullback",
                    detail=(
                        f"{pct_below_high:.0f}% off its 52-week high, below the 20-day "
                        "average but still above the 200-day — dip inside an uptrend"
                    ),
                    score=min(2.5, 1.2 + pct_below_high / 20),
                )
            )
    return events


def _sector_leadership(
    db: Session, series: Series, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """Names leading the strongest sectors.

    Money rotates by sector, so a merely-good chart in a weak sector is worse
    than an equally good chart in a leading one. Sector membership already
    lives in `fundamentals`, and the returns come from the shared series, so
    this ranking costs one extra query.
    """
    rows = db.execute(
        select(FundamentalsRow.symbol, FundamentalsRow.sector).where(
            FundamentalsRow.sector != ""
        )
    ).all()
    sectors: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for symbol, sector in rows:
        if symbol not in universe or symbol not in series:
            continue
        ret = _pct_return([c for c, _ in series[symbol]], p.rs_lookback)
        if ret is not None:
            sectors[sector].append((symbol, ret))

    scored = {
        sector: sorted(r for _, r in members)[len(members) // 2]
        for sector, members in sectors.items()
        if members
    }
    if len(scored) < 2:
        return []  # can't call anything a "leader" without something to lead
    leaders = sorted(scored, key=lambda s: -scored[s])[: p.sector_top_n]

    events: list[DiscoveryEvent] = []
    for sector in leaders:
        median = scored[sector]
        if median <= 0:
            continue  # a sector falling less slowly than the rest is not leadership
        for symbol, ret in sectors[sector]:
            if ret > median:
                events.append(
                    DiscoveryEvent(
                        symbol=symbol,
                        kind="sector_leadership",
                        detail=(
                            f"leads {sector}, one of the strongest sectors "
                            f"({ret:+.0f}% vs the sector's {median:+.0f}%)"
                        ),
                        score=1.0,
                    )
                )
    return events


def _earnings_revisions(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """A streak of consecutive quarterly EPS beats.

    A paid estimate-revision feed would be better, but this is the free,
    deterministic proxy for the same idea — a company repeatedly delivering
    ahead of what analysts modelled — computed entirely from the earnings
    calendar already in the database.
    """
    rows = db.execute(
        select(EarningsCalendarRow).where(
            EarningsCalendarRow.eps_actual.is_not(None),
            EarningsCalendarRow.eps_estimate.is_not(None),
        )
    ).scalars().all()
    history: dict[str, list[tuple[date, float, float]]] = defaultdict(list)
    for r in rows:
        if r.symbol in universe and r.eps_estimate and r.eps_actual is not None:
            history[r.symbol].append((r.date, float(r.eps_actual), float(r.eps_estimate)))

    events = []
    for symbol, quarters in history.items():
        ordered = sorted(quarters, key=lambda q: q[0], reverse=True)
        streak = 0
        for _d, actual, estimate in ordered:
            if actual > estimate:
                streak += 1
            else:
                break
        if streak >= p.earnings_beat_streak:
            events.append(
                DiscoveryEvent(
                    symbol=symbol,
                    kind="earnings_revision",
                    detail=f"{streak} consecutive quarterly EPS beats",
                    score=min(2.5, 1.0 + streak * 0.4),
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


def _latest_closes(db: Session, symbols: set[str]) -> dict[str, float]:
    """Most recent daily close per symbol, from bars already ingested (one
    query for the whole universe, mirroring _volume_and_moves above)."""
    if not symbols:
        return {}
    rows = db.execute(
        select(BarRow.symbol, BarRow.ts, BarRow.close)
        .where(BarRow.timeframe == "1Day", BarRow.symbol.in_(symbols))
        .order_by(BarRow.symbol, BarRow.ts.desc())
    ).all()
    out: dict[str, float] = {}
    for symbol, _ts, close in rows:
        out.setdefault(symbol, float(close))  # first row per symbol (desc) is latest
    return out


def _pullback_from_high(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """Trading well below its own 52-week high — the gradual drawdown that
    unusual_volume/macro_move structurally miss because no single day looks
    unusual. This is what "buy MU when it dipped from $1,200 to $840" needs:
    week52_high already lives in fundamentals (Finnhub), so this costs zero
    new API calls."""
    rows = db.execute(
        select(FundamentalsRow.symbol, FundamentalsRow.week52_high).where(
            FundamentalsRow.week52_high.is_not(None), FundamentalsRow.week52_high > 0
        )
    ).all()
    highs = {sym: high for sym, high in rows if sym in universe}
    closes = _latest_closes(db, set(highs))
    events = []
    for symbol, high in highs.items():
        close = closes.get(symbol)
        if close is None or close <= 0:
            continue
        pct_off = (high - close) / high * 100
        if p.pullback_min_pct <= pct_off <= p.pullback_max_pct:
            events.append(
                DiscoveryEvent(
                    symbol=symbol,
                    kind="pullback_from_high",
                    detail=(
                        f"{pct_off:.0f}% below its 52-week high "
                        f"(${high:,.2f} → ${close:,.2f})"
                    ),
                    score=min(2.5, 1.0 + pct_off / 30),
                )
            )
    return events


def _elevated_short_interest(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """Short interest data only exists for symbols Fintel has already been
    asked about (today's scan set — see ingest_short_interest), so this
    trigger mostly re-confirms already-relevant names rather than finding
    brand-new ones; it still surfaces a real "smart money" signal the other
    triggers can't (crowded shorts, potential squeeze setups)."""
    rows = db.execute(
        select(ShortInterestRow.symbol, ShortInterestRow.short_percent_float)
    ).all()
    events = []
    for symbol, short_pct in rows:
        if symbol not in universe or short_pct is None:
            continue
        if short_pct >= p.short_interest_min_pct:
            events.append(
                DiscoveryEvent(
                    symbol=symbol,
                    kind="elevated_short_interest",
                    detail=f"short interest {short_pct:.1f}% of float",
                    score=min(2.0, short_pct / 15),
                )
            )
    return events


def _finviz_screen(
    db: Session, universe: set[str], p: DiscoveryParams
) -> list[DiscoveryEvent]:
    """One Finviz Elite export call screening the *whole market* for a deep
    pullback from the 52-week high — the only trigger that can surface a
    symbol B-Quant isn't already tracking. Silently contributes nothing when
    Finviz isn't configured (optional enrichment, same as any other
    provider gated by CredentialsMissing)."""
    try:
        screener = build_screener(db)
    except CredentialsMissing:
        return []
    try:
        rows = screener.pullback_candidates(min_pct_below_high=p.finviz_pullback_min_pct)
    except ProviderError as exc:
        log.warning("finviz screen failed", error=str(exc))
        return []
    events = []
    for row in rows:
        detail = f"Finviz: ≥{p.finviz_pullback_min_pct}% below 52-week high"
        if row.price is not None:
            detail += f", ${row.price:,.2f}"
        if row.symbol not in universe:
            detail += " (outside the static universe — new to B-Quant)"
        events.append(
            DiscoveryEvent(symbol=row.symbol, kind="finviz_screen", detail=detail, score=1.5)
        )
    return events


# ------------------------------------------------------------- driver ----
def _passes_quality_gate(
    symbol: str, points: list[tuple[float, int]] | None, p: DiscoveryParams
) -> bool:
    """Drop penny stocks and untradeably thin names from the candidate list.

    Fails OPEN: a symbol with no bars yet (a brand-new discovery from the
    Finviz market-wide screen, say) is kept, because "we have no data" is not
    evidence of poor quality. The screener and the risk engine both fail
    CLOSED further down, so nothing untradeable can reach a signal.
    """
    if not points:
        return True
    last_close, _ = points[-1]
    if last_close < p.min_price:
        log.debug("quality gate: price floor", symbol=symbol, close=last_close)
        return False
    tail = points[-20:]
    if len(tail) >= 20:
        adv = mean(c * v for c, v in tail)
        if adv < p.min_avg_dollar_volume:
            log.debug("quality gate: liquidity floor", symbol=symbol, adv=adv)
            return False
    return True


def discover(db: Session, params: DiscoveryParams | None = None) -> DiscoveryResult:
    """Sweep the universe, rank symbols by summed event score, persist the
    capped candidate list for the day's scans."""
    from sentinel.db.settings_store import set_setting

    p = params or DiscoveryParams()
    universe = set(get_universe(db)) - set(MARKET_SYMBOLS)
    events: list[DiscoveryEvent] = []
    activity: dict[str, float] = {}

    # One bar query serves every technical trigger below.
    series: Series = {}
    try:
        series = _daily_series(db, universe | set(MARKET_SYMBOLS), p.series_lookback_days)
    except Exception:
        log.exception("discovery bar load failed")
    benchmark = [c for c, _ in series.get(MARKET_SYMBOLS[0], [])]
    universe_series: Series = {s: pts for s, pts in series.items() if s in universe}

    try:
        vol_events, activity = _volume_and_moves(universe_series, p)
        events.extend(vol_events)
    except Exception:
        log.exception("discovery trigger failed", trigger="_volume_and_moves")
    try:
        events.extend(_technical_setups(universe_series, benchmark, p))
    except Exception:
        log.exception("discovery trigger failed", trigger="_technical_setups")
    try:
        events.extend(_sector_leadership(db, universe_series, universe, p))
    except Exception:
        log.exception("discovery trigger failed", trigger="_sector_leadership")
    for trigger in (
        _earnings_surprises,
        _earnings_revisions,
        _high_impact_news,
        _insider_clusters,
        _fresh_filings,
        _pullback_from_high,
        _elevated_short_interest,
        _finviz_screen,
    ):
        try:
            events.extend(trigger(db, universe, p))
        except Exception:
            log.exception("discovery trigger failed", trigger=trigger.__name__)

    totals: dict[str, float] = defaultdict(float)
    for e in events:
        totals[e.symbol] += e.score
    # Ties on summed event score (common: flat-score triggers like 8-K
    # filings) break on real market activity, never on the alphabet.
    ranked = sorted(totals, key=lambda s: (-totals[s], -activity.get(s, 0.0), s))
    tradeable = [s for s in ranked if _passes_quality_gate(s, series.get(s), p)]
    dropped = len(ranked) - len(tradeable)
    candidates = tradeable[: p.max_candidates]

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
                f"{len(events)} events, {len(candidates)} candidates "
                f"({dropped} dropped by the quality gate) — 0 LLM calls"
            ),
            payload={"candidates": candidates, "quality_gate_dropped": dropped},
        )
    )
    db.flush()
    log.info(
        "discovery complete",
        events=len(events),
        candidates=candidates,
        quality_gate_dropped=dropped,
    )
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


def technical_focus_set(
    db: Session, limit: int = 60, params: DiscoveryParams | None = None
) -> list[str]:
    """Rank the whole universe on price action alone and return the top slice.

    This is the pre-ingest funnel. Bars are cheap and cover everything; the
    expensive per-symbol provider calls (news, filings, insider transactions,
    fundamentals, quotes) are the ones worth rationing, so they get pointed at
    this shortlist instead of all 700 names. Purely deterministic and free.

    Fails open: with no bars stored yet, an empty list is returned and callers
    fall back to their own defaults rather than ingesting nothing.
    """
    p = params or DiscoveryParams()
    universe = set(get_universe(db)) - set(MARKET_SYMBOLS)
    try:
        series = _daily_series(db, universe | set(MARKET_SYMBOLS), p.series_lookback_days)
    except Exception:
        log.exception("focus-set bar load failed")
        return []
    benchmark = [c for c, _ in series.get(MARKET_SYMBOLS[0], [])]
    bench_return = _pct_return(benchmark, p.rs_lookback) or 0.0

    scored: dict[str, float] = {}
    for symbol, points in series.items():
        if symbol not in universe or not _passes_quality_gate(symbol, points, p):
            continue
        closes = [c for c, _ in points]
        last = closes[-1]
        own = _pct_return(closes, p.rs_lookback)
        score = 0.0
        if own is not None:
            score += own - bench_return
        for period, weight in ((20, 3.0), (50, 5.0), (200, 8.0)):
            sma = _sma(closes, period)
            if sma is not None:
                score += weight if last > sma else -weight
        scored[symbol] = score
    return [s for s, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def get_deep_data_symbols(db: Session, focus_limit: int = 60) -> list[str]:
    """Symbols worth spending per-symbol provider calls on today.

    Technical shortlist + yesterday's discovery candidates + the highlighted
    watchlist + everything held. The union is what the pre-market job fetches
    news, filings, insider transactions, fundamentals and quotes for.
    """
    from sentinel.db.settings_store import get_watchlist

    focus = set(technical_focus_set(db, limit=focus_limit))
    return sorted(
        focus | set(get_candidates(db)) | set(get_watchlist(db)) | set(held_symbols(db))
    )


def get_scan_symbols(db: Session) -> list[str]:
    """What a scheduled scan actually analyzes with the LLM: discovery
    candidates + highlighted watchlist + held positions. The watchlist no
    longer limits anything — it only guarantees its names are always scanned."""
    from sentinel.db.settings_store import get_watchlist

    candidates = get_candidates(db)
    if not candidates:
        log.warning(
            "no fresh discovery candidates (discovery hasn't run in >30h?); "
            "scan set falls back to watchlist + held positions only"
        )
    return sorted(set(candidates) | set(get_watchlist(db)) | set(held_symbols(db)))


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
