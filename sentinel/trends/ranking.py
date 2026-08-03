"""Rank the companies exposed to a theme — and refuse to rank the bad ones.

The instruction this module implements is the one that matters most: *do not
recommend a stock simply because it is trending*. Being in the path of a real
trend is a necessary condition, never a sufficient one. So a name is only
ranked after it clears a hard quality gate, and its rank is then dominated by
company-level facts rather than by how loudly it is being discussed.

Seven factors, each normalised to 0-1 and reported individually:

    financial_health       profitability, leverage proxy, size, data coverage
    revenue_growth         top-line and earnings growth
    momentum               trend position and relative strength
    institutional_interest ETF holdings, accumulation, insider buying
    valuation              multiples ranked WITHIN the theme's own peer set
    competitive_advantage  scale, growth vs peers, sustained relative strength
    risk                   volatility, drawdown, crowding (LOWER is better)

Two guards run before any of that:

**Quality gate** — no penny stocks, no micro-caps, no illiquid names, no
symbols without enough price history to judge. It fails CLOSED for the
price/liquidity floors (missing data is not a pass) because unlike the
discovery engine, this module's output has a dollar amount attached to it.

**Pump-and-dump guard** — the specific signature of a manipulated move: a
violent price spike on abnormal volume in a small, heavily-shorted name that
is being loudly discussed, with no earnings, filing or policy event behind it.
Names that trip it are excluded from recommendation entirely and reported as
excluded, so the user can see what was rejected and why.

Valuation is ranked within the theme's peers rather than against absolute
thresholds: a 40x semiconductor stock and a 40x utility are not the same
statement, and an absolute PE cutoff would systematically exclude every
growth theme the agent exists to find.

Zero LLM calls.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.db.models import (
    EarningsCalendarRow,
    EtfHoldingRow,
    FilingRow,
    FundamentalsRow,
    InsiderTransactionRow,
    ShortInterestRow,
    SocialMentionRow,
)
from sentinel.trends import market
from sentinel.trends.scoring import Accumulation
from sentinel.trends.taxonomy import Theme

log = structlog.get_logger()

# --- quality gate ---------------------------------------------------------
MIN_PRICE = 5.0  # no penny stocks, per the brief
MIN_MARKET_CAP_MILLIONS = 500.0  # no micro-caps
MIN_AVG_DOLLAR_VOLUME = 10_000_000.0  # must be exitable
MIN_BARS = 120  # roughly six months of history

# --- pump-and-dump guard --------------------------------------------------
PUMP_SPIKE_21D_PCT = 80.0  # near-doubling in a month
PUMP_VOLUME_RATIO = 3.0  # on triple normal volume
PUMP_SMALL_CAP_MILLIONS = 3_000.0
PUMP_SHORT_INTEREST_PCT = 20.0
PUMP_SOCIAL_MENTIONS = 8

FactorName = str


class FactorScores(BaseModel):
    financial_health: float = 0.0
    revenue_growth: float = 0.0
    momentum: float = 0.0
    institutional_interest: float = 0.0
    valuation: float = 0.0
    competitive_advantage: float = 0.0
    risk: float = 0.0  # 0 = safest, 1 = riskiest

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 4) for k, v in self.model_dump().items()}


# Weights for the composite. Risk is subtracted, not added.
FACTOR_WEIGHTS: dict[str, float] = {
    "financial_health": 0.20,
    "revenue_growth": 0.18,
    "momentum": 0.18,
    "institutional_interest": 0.16,
    "valuation": 0.14,
    "competitive_advantage": 0.14,
}
RISK_PENALTY = 0.25


class RankedStock(BaseModel):
    """One company's assessment inside one theme."""

    symbol: str
    company: str = ""
    sector: str = ""
    price: float | None = None
    theme: str = ""
    theme_name: str = ""
    trend_connection: str = ""
    factors: FactorScores = Field(default_factory=FactorScores)
    composite: float = 0.0
    confidence: float = 0.0
    risk_level: str = "Medium"
    bullish: list[str] = Field(default_factory=list)
    bearish: list[str] = Field(default_factory=list)
    market_cap_millions: float | None = None
    excluded: bool = False
    exclusion_reason: str = ""
    data_gaps: list[str] = Field(default_factory=list)


class ThemePeerData(BaseModel):
    """Peer statistics used for within-theme percentile ranking."""

    pe_values: dict[str, float] = Field(default_factory=dict)
    ps_values: dict[str, float] = Field(default_factory=dict)
    growth_values: dict[str, float] = Field(default_factory=dict)
    cap_values: dict[str, float] = Field(default_factory=dict)


def _percentile_rank(value: float | None, population: dict[str, float], invert: bool = False) -> float | None:
    """Where `value` sits within its peer group, 0-1. None when unknowable."""
    if value is None or len(population) < 3:
        return None
    values = sorted(population.values())
    below = len([v for v in values if v < value])
    rank = below / max(1, len(values) - 1)
    rank = max(0.0, min(1.0, rank))
    return 1.0 - rank if invert else rank


def _norm(value: float | None, low: float, high: float) -> float | None:
    if value is None or high <= low:
        return None
    return max(0.0, min(1.0, (value - low) / (high - low)))


def load_fundamentals(db: Session, symbols: list[str]) -> dict[str, FundamentalsRow]:
    if not symbols:
        return {}
    rows = db.execute(
        select(FundamentalsRow).where(FundamentalsRow.symbol.in_([s.upper() for s in symbols]))
    ).scalars().all()
    return {r.symbol.upper(): r for r in rows}


def build_peer_data(fundamentals: dict[str, FundamentalsRow]) -> ThemePeerData:
    peers = ThemePeerData()
    for symbol, row in fundamentals.items():
        if row.pe is not None and row.pe > 0:
            peers.pe_values[symbol] = float(row.pe)
        if row.ps is not None and row.ps > 0:
            peers.ps_values[symbol] = float(row.ps)
        if row.revenue_growth_ttm is not None:
            peers.growth_values[symbol] = float(row.revenue_growth_ttm)
        if row.market_cap is not None and row.market_cap > 0:
            peers.cap_values[symbol] = float(row.market_cap)
    return peers


def insider_buyers(db: Session, symbols: list[str], days: int = 90) -> dict[str, int]:
    """Net insider share change per symbol over the window."""
    if not symbols:
        return {}
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        select(InsiderTransactionRow.symbol, InsiderTransactionRow.share_change).where(
            InsiderTransactionRow.symbol.in_([s.upper() for s in symbols]),
            InsiderTransactionRow.transaction_date >= since,
        )
    ).all()
    net: dict[str, int] = {}
    for symbol, change in rows:
        net[symbol.upper()] = net.get(symbol.upper(), 0) + int(change or 0)
    return net


def etf_exposure(db: Session, symbols: list[str]) -> dict[str, int]:
    """How many tracked ETFs currently hold each symbol."""
    if not symbols:
        return {}
    latest = db.execute(
        select(EtfHoldingRow.as_of).order_by(EtfHoldingRow.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return {}
    rows = db.execute(
        select(EtfHoldingRow.symbol, EtfHoldingRow.etf).where(
            EtfHoldingRow.as_of == latest,
            EtfHoldingRow.symbol.in_([s.upper() for s in symbols]),
        )
    ).all()
    counts: dict[str, int] = {}
    for symbol, _etf in rows:
        counts[symbol.upper()] = counts.get(symbol.upper(), 0) + 1
    return counts


def short_interest_map(db: Session, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    rows = db.execute(
        select(ShortInterestRow.symbol, ShortInterestRow.short_percent_float).where(
            ShortInterestRow.symbol.in_([s.upper() for s in symbols])
        )
    ).all()
    return {s.upper(): float(v) for s, v in rows if v is not None}


def social_map(db: Session, symbols: list[str], days: int = 3) -> dict[str, int]:
    if not symbols:
        return {}
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        select(SocialMentionRow.symbol, SocialMentionRow.mentions).where(
            SocialMentionRow.day >= since,
            SocialMentionRow.symbol.in_([s.upper() for s in symbols]),
        )
    ).all()
    out: dict[str, int] = {}
    for symbol, mentions in rows:
        out[symbol.upper()] = out.get(symbol.upper(), 0) + int(mentions or 0)
    return out


def has_hard_catalyst(db: Session, symbol: str, days: int = 10) -> bool:
    """Did something verifiable actually happen — earnings or an 8-K?

    Used by the pump guard to separate "spiked because the business changed"
    from "spiked because a forum decided it should".
    """
    since = date.today() - timedelta(days=days)
    filing = db.execute(
        select(FilingRow.accession_no)
        .where(FilingRow.symbol == symbol.upper(), FilingRow.filed_at >= since)
        .limit(1)
    ).scalar_one_or_none()
    if filing is not None:
        return True
    earnings = db.execute(
        select(EarningsCalendarRow.symbol)
        .where(
            EarningsCalendarRow.symbol == symbol.upper(),
            EarningsCalendarRow.date >= since,
            EarningsCalendarRow.date <= date.today(),
            EarningsCalendarRow.eps_actual.is_not(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    return earnings is not None


# ------------------------------------------------------------- guards ----
def quality_gate(
    symbol: str,
    points: list[tuple[float, int]] | None,
    fundamentals: FundamentalsRow | None,
) -> tuple[bool, str]:
    """Hard floors. Fails CLOSED — this output carries a dollar amount."""
    if not points or len(points) < MIN_BARS:
        return False, f"only {len(points or [])} daily bars — not enough history to judge"
    price = points[-1][0]
    if price < MIN_PRICE:
        return False, f"${price:,.2f} is below the ${MIN_PRICE:,.0f} penny-stock floor"
    tail = points[-20:]
    adv = mean(c * v for c, v in tail)
    if adv < MIN_AVG_DOLLAR_VOLUME:
        return False, f"${adv:,.0f}/day average turnover is below the liquidity floor"
    if fundamentals is None:
        return False, "no fundamentals on file — cannot assess company quality"
    if fundamentals.market_cap is None or fundamentals.market_cap <= 0:
        return False, "market capitalisation unknown"
    if fundamentals.market_cap < MIN_MARKET_CAP_MILLIONS:
        return False, (
            f"${fundamentals.market_cap:,.0f}M market cap is below the "
            f"${MIN_MARKET_CAP_MILLIONS:,.0f}M floor"
        )
    if not (fundamentals.sector or "").strip():
        return False, "sector unknown — the risk engine's concentration rules need it"
    return True, ""


def pump_and_dump_risk(
    symbol: str,
    points: list[tuple[float, int]],
    fundamentals: FundamentalsRow | None,
    short_pct: float | None,
    mentions: int,
    has_catalyst: bool,
) -> tuple[bool, list[str]]:
    """Detect the manipulated-move signature. Returns (is_suspicious, reasons).

    Requires several conditions together, not any one alone: plenty of good
    stocks double, and plenty of good stocks are heavily shorted. It is the
    *combination* of a violent move, abnormal volume, a small float, crowding
    and loud discussion with NO verifiable catalyst that is the tell.
    """
    reasons: list[str] = []
    closes = [c for c, _ in points]
    volumes = [v for _, v in points]

    spike = market.pct_return(closes, 21)
    if spike is not None and spike >= PUMP_SPIKE_21D_PCT:
        reasons.append(f"up {spike:.0f}% in a month")
    if len(volumes) >= 21:
        baseline = mean(volumes[-21:-1])
        if baseline > 0 and volumes[-1] / baseline >= PUMP_VOLUME_RATIO:
            reasons.append(f"trading on {volumes[-1] / baseline:.1f}x normal volume")
    cap = fundamentals.market_cap if fundamentals else None
    if cap is not None and cap < PUMP_SMALL_CAP_MILLIONS:
        reasons.append(f"small ${cap:,.0f}M market cap")
    if short_pct is not None and short_pct >= PUMP_SHORT_INTEREST_PCT:
        reasons.append(f"{short_pct:.0f}% of float sold short")
    if mentions >= PUMP_SOCIAL_MENTIONS:
        reasons.append(f"{mentions} social mentions in three days")

    # A real catalyst explains the move; without one, the pattern stands.
    if has_catalyst:
        return False, reasons

    price_flag = any(r.startswith("up ") for r in reasons)
    crowd_flag = any("short" in r or "mentions" in r for r in reasons)
    return (price_flag and crowd_flag and len(reasons) >= 3), reasons


# ------------------------------------------------------------ factors ----
def _financial_health(row: FundamentalsRow | None, gaps: list[str]) -> float:
    if row is None:
        gaps.append("fundamentals")
        return 0.35  # unknown is neither good nor bad, but never rewarded
    score = 0.5
    if row.pe is not None:
        # Positive PE means positive earnings, which is the single cleanest
        # free proxy for "this is a real business".
        score += 0.2 if row.pe > 0 else -0.25
    else:
        gaps.append("earnings")
    if row.eps_growth_ttm is not None:
        score += max(-0.15, min(0.2, row.eps_growth_ttm / 100.0))
    cap = _norm(row.market_cap, 500.0, 200_000.0)
    if cap is not None:
        score += cap * 0.15
    if row.beta is not None and row.beta > 2.0:
        score -= 0.1
    return max(0.0, min(1.0, score))


def _revenue_growth(row: FundamentalsRow | None, peers: ThemePeerData, symbol: str, gaps: list[str]) -> float:
    if row is None or row.revenue_growth_ttm is None:
        gaps.append("revenue growth")
        return 0.35
    growth = float(row.revenue_growth_ttm)
    absolute = _norm(growth, -10.0, 40.0) or 0.0
    relative = _percentile_rank(growth, peers.growth_values)
    return absolute * 0.6 + (relative if relative is not None else absolute) * 0.4


def _momentum(snap: TechnicalSnapshot | None, excess: float | None, gaps: list[str]) -> float:
    if snap is None:
        gaps.append("technicals")
        return 0.35
    score = 0.0
    for flag, weight in ((snap.above_sma20, 0.12), (snap.above_sma50, 0.18), (snap.above_sma200, 0.25)):
        if flag is True:
            score += weight
        elif flag is None:
            gaps.append("moving averages")
    if excess is not None:
        score += (_norm(excess, -20.0, 30.0) or 0.0) * 0.3
    if snap.rsi14 is not None:
        # Reward strength, penalise the extremes at both ends.
        if 45 <= snap.rsi14 <= 72:
            score += 0.15
        elif snap.rsi14 > 82:
            score -= 0.1
    return max(0.0, min(1.0, score))


def _institutional_interest(
    etf_count: int, accumulating: bool, insider_net: int | None, gaps: list[str]
) -> float:
    score = 0.3
    if etf_count:
        score += min(0.3, etf_count * 0.1)
    else:
        gaps.append("etf holdings")
    if accumulating:
        score += 0.25
    if insider_net is None:
        gaps.append("insider filings")
    elif insider_net > 0:
        score += 0.2
    elif insider_net < 0:
        score -= 0.1
    return max(0.0, min(1.0, score))


def _valuation(row: FundamentalsRow | None, peers: ThemePeerData, symbol: str, gaps: list[str]) -> float:
    """Cheapness relative to the theme's own peers. Higher = better value."""
    ranks: list[float] = []
    pe = float(row.pe) if row and row.pe is not None and row.pe > 0 else None
    ps = float(row.ps) if row and row.ps is not None and row.ps > 0 else None
    pe_rank = _percentile_rank(pe, peers.pe_values, invert=True)
    ps_rank = _percentile_rank(ps, peers.ps_values, invert=True)
    if pe_rank is not None:
        ranks.append(pe_rank)
    if ps_rank is not None:
        ranks.append(ps_rank)
    if not ranks:
        gaps.append("valuation multiples")
        return 0.4
    return sum(ranks) / len(ranks)


def _competitive_advantage(
    row: FundamentalsRow | None, peers: ThemePeerData, symbol: str, excess_63: float | None
) -> float:
    """A PROXY, and labelled as one wherever it surfaces.

    Real moat analysis needs segment economics, pricing power and returns on
    incremental capital — none of which is available free. What IS available:
    scale within the peer set, growth faster than peers, and price outperforming
    peers over a quarter. Those correlate with competitive position without
    pretending to measure it.
    """
    parts: list[float] = []
    if row is not None and row.market_cap is not None:
        scale = _percentile_rank(float(row.market_cap), peers.cap_values)
        if scale is not None:
            parts.append(scale)
    if row is not None and row.revenue_growth_ttm is not None:
        rel_growth = _percentile_rank(float(row.revenue_growth_ttm), peers.growth_values)
        if rel_growth is not None:
            parts.append(rel_growth)
    if excess_63 is not None:
        parts.append(_norm(excess_63, -25.0, 35.0) or 0.0)
    if not parts:
        return 0.4
    return sum(parts) / len(parts)


def _risk(
    snap: TechnicalSnapshot | None,
    row: FundamentalsRow | None,
    short_pct: float | None,
    pump_reasons: list[str],
) -> tuple[float, str]:
    """0 = safest, 1 = riskiest, plus the Low/Medium/High label."""
    score = 0.3
    if snap is not None and snap.atr_pct is not None:
        score += (_norm(snap.atr_pct, 1.5, 8.0) or 0.0) * 0.3
    if row is not None and row.beta is not None:
        score += (_norm(row.beta, 0.8, 2.5) or 0.0) * 0.2
    if row is not None and row.market_cap is not None:
        # Small caps are riskier; the norm is inverted.
        score += (1.0 - (_norm(row.market_cap, 500.0, 100_000.0) or 0.0)) * 0.2
    if short_pct is not None:
        score += (_norm(short_pct, 5.0, 30.0) or 0.0) * 0.15
    if snap is not None and snap.pct_from_52w_high is not None and snap.pct_from_52w_high > 40:
        score += 0.1
    score += min(0.15, len(pump_reasons) * 0.04)
    score = max(0.0, min(1.0, score))
    label = "Low" if score < 0.35 else ("Medium" if score < 0.6 else "High")
    return score, label


def _trend_connection(
    symbol: str, theme: Theme, mentions: int, etf_count: int, accumulating: bool
) -> str:
    """Why this company is connected to this theme, in plain words."""
    parts: list[str] = []
    if symbol.upper() in {s.upper() for s in theme.seeds}:
        parts.append(f"a core {theme.name.lower()} name")
    if etf_count:
        parts.append(f"held by {etf_count} tracked thematic ETF{'s' if etf_count > 1 else ''}")
    if accumulating:
        parts.append("thematic ETFs increased their position")
    if mentions:
        parts.append(f"named in {mentions} theme-matched article{'s' if mentions > 1 else ''}")
    if not parts:
        parts.append(f"exposed to {theme.name.lower()}")
    return "; ".join(parts).capitalize()


def rank_theme_stocks(
    db: Session,
    theme: Theme,
    symbols: list[str],
    series: market.Series,
    benchmark_closes: list[float],
    snapshots: dict[str, TechnicalSnapshot] | None = None,
    accumulation: list[Accumulation] | None = None,
    theme_mentions: dict[str, int] | None = None,
    limit: int = 8,
) -> tuple[list[RankedStock], list[RankedStock]]:
    """Rank a theme's constituents. Returns (ranked, excluded).

    `excluded` is returned rather than discarded so the report and the UI can
    show what was rejected and why — a system that silently drops names is one
    the user cannot audit.
    """
    if not symbols:
        return [], []

    fundamentals = load_fundamentals(db, symbols)
    peers = build_peer_data(fundamentals)
    etf_counts = etf_exposure(db, symbols)
    insiders = insider_buyers(db, symbols)
    shorts = short_interest_map(db, symbols)
    mentions_social = social_map(db, symbols)
    accumulating_symbols = {a.symbol.upper() for a in (accumulation or [])}
    mentions_news = theme_mentions or {}
    snaps = snapshots or {}

    bench_21 = market.pct_return(benchmark_closes, 21)
    bench_63 = market.pct_return(benchmark_closes, 63)

    ranked: list[RankedStock] = []
    excluded: list[RankedStock] = []

    for symbol in symbols:
        upper = symbol.upper()
        points = series.get(upper)
        row = fundamentals.get(upper)

        base = RankedStock(
            symbol=upper,
            company=(row.name if row else "") or upper,
            sector=(row.sector if row else "") or "",
            price=points[-1][0] if points else None,
            theme=theme.key,
            theme_name=theme.name,
            market_cap_millions=float(row.market_cap) if row and row.market_cap else None,
        )

        ok, reason = quality_gate(upper, points, row)
        if not ok:
            base.excluded = True
            base.exclusion_reason = reason
            excluded.append(base)
            continue

        assert points is not None  # quality_gate guarantees it
        closes = [c for c, _ in points]
        short_pct = shorts.get(upper)
        suspicious, pump_reasons = pump_and_dump_risk(
            upper,
            points,
            row,
            short_pct,
            mentions_social.get(upper, 0),
            has_hard_catalyst(db, upper),
        )
        if suspicious:
            base.excluded = True
            base.exclusion_reason = (
                "excluded as a possible pump: " + ", ".join(pump_reasons) + " — with no earnings "
                "release or SEC filing to explain the move"
            )
            excluded.append(base)
            continue

        own_21 = market.pct_return(closes, 21)
        own_63 = market.pct_return(closes, 63)
        excess_21 = own_21 - bench_21 if own_21 is not None and bench_21 is not None else None
        excess_63 = own_63 - bench_63 if own_63 is not None and bench_63 is not None else None

        gaps: list[str] = []
        snap = snaps.get(upper)
        accumulating = upper in accumulating_symbols

        factors = FactorScores(
            financial_health=_financial_health(row, gaps),
            revenue_growth=_revenue_growth(row, peers, upper, gaps),
            momentum=_momentum(snap, excess_21, gaps),
            institutional_interest=_institutional_interest(
                etf_counts.get(upper, 0), accumulating, insiders.get(upper), gaps
            ),
            valuation=_valuation(row, peers, upper, gaps),
            competitive_advantage=_competitive_advantage(row, peers, upper, excess_63),
        )
        risk_score, risk_label = _risk(snap, row, short_pct, pump_reasons)
        factors.risk = risk_score

        composite = sum(
            getattr(factors, name) * weight for name, weight in FACTOR_WEIGHTS.items()
        ) - risk_score * RISK_PENALTY
        composite = max(0.0, min(1.0, composite + RISK_PENALTY * 0.5))

        base.factors = factors
        base.composite = round(composite, 4)
        base.risk_level = risk_label
        base.data_gaps = sorted(set(gaps))
        # Confidence falls with missing data: a high score built on three
        # unknowns is not the same claim as one built on complete data.
        base.confidence = round(max(0.0, composite * (1.0 - 0.06 * len(base.data_gaps))), 4)
        base.trend_connection = _trend_connection(
            upper, theme, mentions_news.get(upper, 0), etf_counts.get(upper, 0), accumulating
        )
        base.bullish, base.bearish = _factor_narrative(
            base, row, snap, excess_21, etf_counts.get(upper, 0), accumulating,
            insiders.get(upper), short_pct, pump_reasons
        )
        ranked.append(base)

    ranked.sort(key=lambda s: -s.composite)
    return ranked[:limit], excluded


def _factor_narrative(
    stock: RankedStock,
    row: FundamentalsRow | None,
    snap: TechnicalSnapshot | None,
    excess_21: float | None,
    etf_count: int,
    accumulating: bool,
    insider_net: int | None,
    short_pct: float | None,
    pump_reasons: list[str],
) -> tuple[list[str], list[str]]:
    """Concrete bullish/bearish points, each tied to a number the user can check."""
    bullish: list[str] = []
    bearish: list[str] = []

    if row is not None:
        if row.revenue_growth_ttm is not None:
            if row.revenue_growth_ttm >= 10:
                bullish.append(f"revenue growing {row.revenue_growth_ttm:.0f}% over the last year")
            elif row.revenue_growth_ttm < 0:
                bearish.append(f"revenue shrinking {abs(row.revenue_growth_ttm):.0f}% over the last year")
        if row.pe is not None:
            if row.pe <= 0:
                bearish.append("not currently profitable on a trailing basis")
            elif row.pe > 60:
                bearish.append(f"expensive at {row.pe:.0f}x trailing earnings")
            elif row.pe < 25:
                bullish.append(f"reasonably valued at {row.pe:.0f}x trailing earnings")
        if row.market_cap is not None and row.market_cap >= 20_000:
            bullish.append(f"established ${row.market_cap / 1000:,.0f}B company")

    if excess_21 is not None:
        if excess_21 >= 5:
            bullish.append(f"outperforming the market by {excess_21:.0f} points over the last month")
        elif excess_21 <= -5:
            bearish.append(f"lagging the market by {abs(excess_21):.0f} points over the last month")
    if snap is not None:
        if snap.above_sma200 is True:
            bullish.append("trading above its 200-day average — long-term trend intact")
        elif snap.above_sma200 is False:
            bearish.append("below its 200-day average — long-term trend is broken")
        if snap.atr_pct is not None and snap.atr_pct > 5:
            bearish.append(f"volatile: daily range averages {snap.atr_pct:.1f}% of the price")
        if snap.pct_from_52w_high is not None and snap.pct_from_52w_high > 35:
            bearish.append(f"{snap.pct_from_52w_high:.0f}% below its 52-week high")

    if etf_count:
        bullish.append(f"held by {etf_count} tracked thematic ETF{'s' if etf_count > 1 else ''}")
    if accumulating:
        bullish.append("thematic ETFs have increased their position since the last snapshot")
    if insider_net is not None and insider_net > 0:
        bullish.append(f"insiders net buyers ({insider_net:,} shares over 90 days)")
    elif insider_net is not None and insider_net < 0:
        bearish.append(f"insiders net sellers ({abs(insider_net):,} shares over 90 days)")
    if short_pct is not None and short_pct >= 15:
        bearish.append(f"{short_pct:.0f}% of the float is sold short — crowded")
    if pump_reasons and not stock.excluded:
        bearish.append("some fast-money characteristics: " + ", ".join(pump_reasons))
    if stock.data_gaps:
        bearish.append("incomplete data on " + ", ".join(stock.data_gaps))

    return bullish[:6], bearish[:6]
