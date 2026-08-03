"""Trend strength scoring and the hype guard.

A trend's strength is a 0-100 number built from six components that are
weighted, bounded, and individually reported. Nothing here is a black box:
every score decomposes into sub-scores with the evidence that produced them,
because a number a user cannot interrogate is a number they cannot sensibly
act on.

    news_momentum        coverage now vs this theme's own recent baseline
    policy_support       government rules, awards and agency activity
    market_confirmation  did the constituent basket actually outperform
    etf_activity         thematic ETF strength + accumulation in holdings
    social_attention     mention growth and sentiment
    breadth              how many members participate, not just the leader

Two design decisions matter more than the weights:

**Talk is discounted against action.** `market_confirmation` and
`policy_support` together outweigh `news_momentum` and `social_attention`
together. A theme cannot reach a high score on coverage alone.

**Hype is scored explicitly, not assumed away.** `assess_legitimacy` looks for
the specific signature of a hype cycle — social dominating every other
component, a basket carried by one name, no policy or market confirmation —
and CAPS the strength score when it finds it. The cap is one-directional: the
hype guard can only lower a trend's score, never raise it.

Zero LLM calls. This module runs over every theme every day.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import mean

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import EtfHoldingRow, SocialMentionRow, TrendDocumentRow, TrendSnapshotRow
from sentinel.trends import market
from sentinel.trends.taxonomy import THEMES, Theme

log = structlog.get_logger()

# --- component weights (sum to 100) ---------------------------------------
# Action outweighs talk: market + policy = 45, news + social = 32.
WEIGHTS: dict[str, float] = {
    "news_momentum": 22.0,
    "policy_support": 18.0,
    "market_confirmation": 25.0,
    "etf_activity": 15.0,
    "social_attention": 10.0,
    "breadth": 10.0,
}

# Policy-driven themes (defense, nuclear, infrastructure) reallocate weight
# from social chatter to government evidence, because that is genuinely where
# their information is.
POLICY_THEME_SHIFT = 6.0

RECENT_WINDOW_DAYS = 3  # "now"
BASELINE_WINDOW_DAYS = 21  # what "now" is compared against
PERSISTENCE_WINDOW_DAYS = 10

Legitimacy = str  # "legitimate" | "emerging" | "mixed" | "hype" | "unproven"


class ComponentScore(BaseModel):
    """One 0-100 sub-score plus the human-readable reason for it."""

    name: str
    score: float = 0.0
    weight: float = 0.0
    detail: str = ""
    covered: bool = True  # False = we could not measure this, not "it was zero"

    @property
    def contribution(self) -> float:
        return self.score / 100.0 * self.weight


class TrendScore(BaseModel):
    """A theme's full assessment for one day."""

    theme: str
    name: str
    day: date
    score: float = 0.0
    raw_score: float = 0.0  # before the hype cap
    legitimacy: Legitimacy = "unproven"
    components: list[ComponentScore] = Field(default_factory=list)
    explanation: str = ""
    hype_flags: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    symbols: list[str] = Field(default_factory=list)
    market_read: market.ThemeMarketRead | None = None

    def component(self, name: str) -> ComponentScore | None:
        for c in self.components:
            if c.name == name:
                return c
        return None

    @property
    def coverage_gaps(self) -> list[str]:
        return [c.name for c in self.components if not c.covered]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _saturating(value: float, midpoint: float) -> float:
    """Map an unbounded positive measure onto 0-100 with diminishing returns.

    A theme with 200 articles is not twice as real as one with 100, so a
    linear scale would let a single noisy day dominate. `midpoint` is the
    value that scores 50.
    """
    if value <= 0 or midpoint <= 0:
        return 0.0
    return _clamp(100.0 * value / (value + midpoint))


def _weights_for(theme: Theme) -> dict[str, float]:
    weights = dict(WEIGHTS)
    if theme.policy_driven:
        shift = min(POLICY_THEME_SHIFT, weights["social_attention"])
        weights["social_attention"] -= shift
        weights["policy_support"] += shift
    return weights


# ---------------------------------------------------------------- data ----
class ThemeCorpus(BaseModel):
    """Per-theme document counts, split by channel and window. Built once for
    all themes so scoring never re-queries per theme."""

    recent_news: dict[str, int] = Field(default_factory=dict)
    baseline_news: dict[str, int] = Field(default_factory=dict)
    recent_gov: dict[str, int] = Field(default_factory=dict)
    baseline_gov: dict[str, int] = Field(default_factory=dict)
    recent_social: dict[str, int] = Field(default_factory=dict)
    baseline_social: dict[str, int] = Field(default_factory=dict)
    news_sentiment: dict[str, list[float]] = Field(default_factory=dict)
    social_sentiment: dict[str, list[float]] = Field(default_factory=dict)
    symbol_mentions: dict[str, dict[str, int]] = Field(default_factory=dict)
    headlines: dict[str, list[str]] = Field(default_factory=dict)
    gov_headlines: dict[str, list[str]] = Field(default_factory=dict)
    total_recent_docs: int = 0


def build_corpus(db: Session, now: datetime | None = None) -> ThemeCorpus:
    """Aggregate every stored document into per-theme windows. One query."""
    reference = now or datetime.now(UTC)
    recent_cutoff = reference - timedelta(days=RECENT_WINDOW_DAYS)
    baseline_cutoff = reference - timedelta(days=BASELINE_WINDOW_DAYS)

    corpus = ThemeCorpus()
    rows = db.execute(
        select(
            TrendDocumentRow.channel,
            TrendDocumentRow.themes,
            TrendDocumentRow.symbols,
            TrendDocumentRow.sentiment,
            TrendDocumentRow.engagement,
            TrendDocumentRow.title,
            TrendDocumentRow.published_at,
        ).where(TrendDocumentRow.published_at >= baseline_cutoff)
    ).all()

    for channel, themes, symbols, score, _engagement, title, published in rows:
        stamp = published if published.tzinfo else published.replace(tzinfo=UTC)
        is_recent = stamp >= recent_cutoff
        if is_recent:
            corpus.total_recent_docs += 1
        for theme_key in themes or []:
            if channel == "news":
                bucket = corpus.recent_news if is_recent else corpus.baseline_news
                if is_recent:
                    corpus.news_sentiment.setdefault(theme_key, []).append(float(score or 0))
                    corpus.headlines.setdefault(theme_key, []).append(str(title))
            elif channel == "gov":
                bucket = corpus.recent_gov if is_recent else corpus.baseline_gov
                if is_recent:
                    corpus.gov_headlines.setdefault(theme_key, []).append(str(title))
            else:
                bucket = corpus.recent_social if is_recent else corpus.baseline_social
                if is_recent:
                    corpus.social_sentiment.setdefault(theme_key, []).append(float(score or 0))
            bucket[theme_key] = bucket.get(theme_key, 0) + 1

            if is_recent:
                per_theme = corpus.symbol_mentions.setdefault(theme_key, {})
                for symbol in symbols or []:
                    per_theme[str(symbol).upper()] = per_theme.get(str(symbol).upper(), 0) + 1

    return corpus


# --------------------------------------------------------- ETF holdings ----
class Accumulation(BaseModel):
    """A symbol whose ETF exposure grew between two snapshots."""

    symbol: str
    etf: str
    weight_now: float | None = None
    weight_before: float | None = None
    weight_change: float | None = None
    shares_change_pct: float | None = None
    newly_added: bool = False


def etf_accumulation(
    db: Session,
    etfs: list[str],
    lookback_days: int = 21,
    min_weight_change: float = 0.25,
    min_relative_weight_change: float = 0.25,
) -> list[Accumulation]:
    """Symbols these ETFs increased exposure to between the two most recent
    snapshots at least `lookback_days` apart.

    This is the literal answer to "what uranium companies are ETFs increasing
    exposure to?" — but only for ETFs whose issuer publishes holdings for
    free. Callers must treat an empty list as "no free holdings data", never
    as "no accumulation".

    Two thresholds, because index weights drift constantly with price alone:
    an increase counts if it is either large in ABSOLUTE terms (0.25 of a
    percentage point) or large RELATIVE to the existing position (+25%). The
    second catches a conviction move in a small holding — 0.4% to 0.9% of the
    fund is a real decision — that an absolute-only test would miss, while the
    first stops a 20%-weight mega-cap qualifying on ordinary price drift.
    """
    if not etfs:
        return []
    upper = [e.upper() for e in etfs]
    dates = db.execute(
        select(EtfHoldingRow.as_of)
        .where(EtfHoldingRow.etf.in_(upper))
        .distinct()
        .order_by(EtfHoldingRow.as_of.desc())
    ).scalars().all()
    if len(dates) < 2:
        return []

    latest = dates[0]
    target = latest - timedelta(days=lookback_days)
    # Newest snapshot that is at least lookback_days old; else the oldest we
    # have, so a young install still produces a comparison rather than silence.
    earlier = next((d for d in dates[1:] if d <= target), dates[-1])
    if earlier >= latest:
        return []

    def _snapshot(day: date) -> dict[tuple[str, str], tuple[float | None, float | None]]:
        rows = db.execute(
            select(
                EtfHoldingRow.etf,
                EtfHoldingRow.symbol,
                EtfHoldingRow.weight_pct,
                EtfHoldingRow.shares,
            ).where(EtfHoldingRow.etf.in_(upper), EtfHoldingRow.as_of == day)
        ).all()
        return {(etf, symbol): (weight, shares) for etf, symbol, weight, shares in rows}

    now_snap = _snapshot(latest)
    then_snap = _snapshot(earlier)

    out: list[Accumulation] = []
    for (etf, symbol), (weight_now, shares_now) in now_snap.items():
        weight_before, shares_before = then_snap.get((etf, symbol), (None, None))
        newly_added = (etf, symbol) not in then_snap
        weight_change = (
            round(weight_now - weight_before, 4)
            if weight_now is not None and weight_before is not None
            else None
        )
        shares_change = (
            round((shares_now / shares_before - 1) * 100, 2)
            if shares_now and shares_before and shares_before > 0
            else None
        )
        relative_weight_change = (
            weight_change / weight_before
            if weight_change is not None and weight_before
            else None
        )
        increased = (
            newly_added
            # Absolute OR relative — see the docstring on why both are needed.
            or (weight_change is not None and weight_change >= min_weight_change)
            or (
                relative_weight_change is not None
                and relative_weight_change >= min_relative_weight_change
            )
            # Share count is the cleanest signal when it is published: it moves
            # only when the fund actually transacts, not when the price does.
            or (shares_change is not None and shares_change >= 5.0)
        )
        if increased:
            out.append(
                Accumulation(
                    symbol=symbol,
                    etf=etf,
                    weight_now=weight_now,
                    weight_before=weight_before,
                    weight_change=weight_change,
                    shares_change_pct=shares_change,
                    newly_added=newly_added,
                )
            )
    return sorted(out, key=lambda a: -(a.weight_change or 0))


def etf_members(db: Session, etfs: list[str], limit_per_etf: int = 60) -> set[str]:
    """Current constituents of the given ETFs, from the newest snapshot."""
    if not etfs:
        return set()
    upper = [e.upper() for e in etfs]
    latest = db.execute(
        select(EtfHoldingRow.as_of)
        .where(EtfHoldingRow.etf.in_(upper))
        .order_by(EtfHoldingRow.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return set()
    members: set[str] = set()
    for etf in upper:
        rows = db.execute(
            select(EtfHoldingRow.symbol)
            .where(EtfHoldingRow.etf == etf, EtfHoldingRow.as_of == latest)
            .order_by(EtfHoldingRow.weight_pct.desc().nullslast())
            .limit(limit_per_etf)
        ).scalars().all()
        members |= {str(s).upper() for s in rows}
    return members


# ------------------------------------------------------------- social ----
def social_growth(db: Session, symbols: set[str], now: date | None = None) -> dict:
    """Mention volume now vs baseline for a set of symbols.

    Growth, not level: a theme whose names are permanently discussed (AI) must
    not out-score one that just started being discussed, because the second is
    the actual discovery.
    """
    today = now or date.today()
    recent_start = today - timedelta(days=RECENT_WINDOW_DAYS)
    baseline_start = today - timedelta(days=BASELINE_WINDOW_DAYS)
    if not symbols:
        return {"recent": 0, "baseline_daily": 0.0, "growth_pct": None, "sentiment": 0.0}

    rows = db.execute(
        select(
            SocialMentionRow.symbol,
            SocialMentionRow.day,
            SocialMentionRow.mentions,
            SocialMentionRow.sentiment,
        ).where(SocialMentionRow.day >= baseline_start, SocialMentionRow.symbol.in_(symbols))
    ).all()

    recent_total = 0
    baseline_total = 0
    recent_sentiment: list[float] = []
    for _symbol, day, mentions, score in rows:
        if day >= recent_start:
            recent_total += int(mentions or 0)
            recent_sentiment.append(float(score or 0.0))
        else:
            baseline_total += int(mentions or 0)

    baseline_days = max(1, (recent_start - baseline_start).days)
    baseline_daily = baseline_total / baseline_days
    recent_daily = recent_total / max(1, RECENT_WINDOW_DAYS)
    growth = (
        round((recent_daily / baseline_daily - 1) * 100, 2) if baseline_daily > 0 else None
    )
    return {
        "recent": recent_total,
        "baseline_daily": round(baseline_daily, 2),
        "recent_daily": round(recent_daily, 2),
        "growth_pct": growth,
        "sentiment": round(mean(recent_sentiment), 4) if recent_sentiment else 0.0,
    }


# ---------------------------------------------------------- components ----
def _score_news(theme: Theme, corpus: ThemeCorpus, weight: float) -> ComponentScore:
    recent = corpus.recent_news.get(theme.key, 0)
    baseline = corpus.baseline_news.get(theme.key, 0)
    baseline_days = max(1, BASELINE_WINDOW_DAYS - RECENT_WINDOW_DAYS)
    baseline_daily = baseline / baseline_days
    recent_daily = recent / max(1, RECENT_WINDOW_DAYS)

    if recent == 0 and baseline == 0:
        return ComponentScore(
            name="news_momentum",
            score=0.0,
            weight=weight,
            covered=corpus.total_recent_docs > 0,
            detail=(
                "no news collected for this theme"
                if corpus.total_recent_docs
                else "no news sources answered today"
            ),
        )

    # Volume alone scores up to 60; acceleration against the theme's own
    # baseline adds the rest, so a theme that is merely always-covered cannot
    # reach the top of the range.
    volume_score = _saturating(recent_daily, midpoint=6.0) * 0.6
    if baseline_daily > 0:
        acceleration = recent_daily / baseline_daily
        accel_score = _clamp((acceleration - 1.0) * 60.0, 0.0, 40.0)
    else:
        accel_score = 40.0 if recent_daily >= 2 else 20.0

    sentiment_scores = corpus.news_sentiment.get(theme.key, [])
    tone = mean(sentiment_scores) if sentiment_scores else 0.0
    # Negative coverage is still a trend, but a weaker investable one.
    tone_multiplier = 1.0 if tone >= 0 else max(0.6, 1.0 + tone * 0.4)

    score = _clamp((volume_score + accel_score) * tone_multiplier)
    detail = (
        f"{recent} articles in {RECENT_WINDOW_DAYS}d "
        f"({recent_daily:.1f}/day vs {baseline_daily:.1f}/day baseline)"
    )
    if sentiment_scores:
        detail += f", tone {tone:+.2f}"
    return ComponentScore(name="news_momentum", score=round(score, 2), weight=weight, detail=detail)


def _score_policy(theme: Theme, corpus: ThemeCorpus, weight: float, gov_covered: bool) -> ComponentScore:
    recent = corpus.recent_gov.get(theme.key, 0)
    baseline = corpus.baseline_gov.get(theme.key, 0)
    if not gov_covered:
        return ComponentScore(
            name="policy_support",
            score=0.0,
            weight=weight,
            covered=False,
            detail="no government source answered — policy support could not be measured",
        )
    if recent == 0 and baseline == 0:
        return ComponentScore(
            name="policy_support",
            score=0.0,
            weight=weight,
            detail="no matching government activity in the window",
        )
    # Rulemaking is slow, so raw count over three weeks matters more than
    # acceleration; the midpoint is set low because five relevant federal
    # documents in a week is genuinely a lot for one theme.
    score = _saturating(recent * 2.0 + baseline * 0.5, midpoint=6.0)
    return ComponentScore(
        name="policy_support",
        score=round(score, 2),
        weight=weight,
        detail=(
            f"{recent} federal/agency document(s) in {RECENT_WINDOW_DAYS}d, "
            f"{baseline} more in the prior window"
        ),
    )


def _score_market(read: market.ThemeMarketRead, weight: float) -> ComponentScore:
    basket = read.basket
    if not basket.symbols_with_data:
        return ComponentScore(
            name="market_confirmation",
            score=0.0,
            weight=weight,
            covered=False,
            detail="no price history for this theme's constituents",
        )
    excess_21 = basket.excess_21d_pct
    excess_63 = basket.excess_63d_pct
    if excess_21 is None:
        return ComponentScore(
            name="market_confirmation",
            score=0.0,
            weight=weight,
            covered=False,
            detail="not enough benchmark history to compare against",
        )
    # 50 = matching the market. Every point of monthly excess return adds 2.5,
    # with the quarterly figure worth half as much (confirmation, not novelty).
    score = 50.0 + excess_21 * 2.5 + (excess_63 or 0.0) * 1.25
    # A basket that is up on the mean but flat on the median is one stock.
    if (
        basket.median_return_21d_pct is not None
        and basket.return_21d_pct is not None
        and basket.return_21d_pct > 0
        and basket.median_return_21d_pct <= 0
    ):
        score -= 15.0
    detail = (
        f"basket {basket.return_21d_pct:+.1f}% over 21 sessions "
        f"({excess_21:+.1f}% vs benchmark), median {basket.median_return_21d_pct:+.1f}%, "
        f"{basket.participation_pct:.0f}% of members outperforming"
    )
    return ComponentScore(
        name="market_confirmation", score=round(_clamp(score), 2), weight=weight, detail=detail
    )


def _score_etf(
    read: market.ThemeMarketRead, accumulation: list[Accumulation], weight: float
) -> ComponentScore:
    if not read.etfs:
        return ComponentScore(
            name="etf_activity",
            score=0.0,
            weight=weight,
            covered=False,
            detail="no thematic ETF bars available for this theme",
        )
    excesses = [e.excess_21d_pct for e in read.etfs if e.excess_21d_pct is not None]
    turnover = [e.dollar_volume_trend_pct for e in read.etfs if e.dollar_volume_trend_pct is not None]

    score = 50.0
    if excesses:
        score += mean(excesses) * 2.5
    if turnover:
        # Rising turnover is the closest free proxy for inflows.
        score += _clamp(mean(turnover), -25.0, 25.0) * 0.6
    accumulating = read.etfs_accumulating
    score += len(accumulating) * 4.0
    if accumulation:
        score += min(10.0, len(accumulation) * 1.0)

    detail_parts = []
    if excesses:
        detail_parts.append(f"ETFs {mean(excesses):+.1f}% vs benchmark over 21 sessions")
    if turnover:
        detail_parts.append(f"turnover {mean(turnover):+.0f}% vs its own baseline")
    if accumulating:
        detail_parts.append(f"accumulating: {', '.join(accumulating)}")
    if accumulation:
        names = ", ".join(sorted({a.symbol for a in accumulation})[:6])
        detail_parts.append(f"holdings increased in {names}")
    else:
        detail_parts.append("no free holdings data — price/volume proxy only")

    return ComponentScore(
        name="etf_activity",
        score=round(_clamp(score), 2),
        weight=weight,
        detail="; ".join(detail_parts),
    )


def _score_social(
    theme: Theme, corpus: ThemeCorpus, growth: dict, weight: float, social_covered: bool
) -> ComponentScore:
    if not social_covered:
        return ComponentScore(
            name="social_attention",
            score=0.0,
            weight=weight,
            covered=False,
            detail="no social source answered — attention could not be measured",
        )
    recent = corpus.recent_social.get(theme.key, 0)
    baseline = corpus.baseline_social.get(theme.key, 0)
    baseline_days = max(1, BASELINE_WINDOW_DAYS - RECENT_WINDOW_DAYS)
    baseline_daily = baseline / baseline_days
    recent_daily = recent / max(1, RECENT_WINDOW_DAYS)

    if recent == 0 and not growth.get("recent"):
        return ComponentScore(
            name="social_attention",
            score=0.0,
            weight=weight,
            detail="no discussion found for this theme",
        )

    volume_score = _saturating(recent_daily + growth.get("recent_daily", 0.0), midpoint=8.0) * 0.5
    theme_growth = (recent_daily / baseline_daily - 1) * 100 if baseline_daily > 0 else None
    symbol_growth = growth.get("growth_pct")
    growth_values = [g for g in (theme_growth, symbol_growth) if g is not None]
    growth_score = _clamp(mean(growth_values) * 0.5, 0.0, 50.0) if growth_values else 25.0

    tone_scores = corpus.social_sentiment.get(theme.key, [])
    tone = mean(tone_scores) if tone_scores else growth.get("sentiment", 0.0)
    tone_multiplier = 1.0 if tone >= 0 else max(0.5, 1.0 + tone * 0.5)

    score = _clamp((volume_score + growth_score) * tone_multiplier)
    detail = (
        f"{recent} theme posts + {growth.get('recent', 0)} ticker mentions in "
        f"{RECENT_WINDOW_DAYS}d"
    )
    if symbol_growth is not None:
        detail += f", mentions {symbol_growth:+.0f}% vs baseline"
    detail += f", tone {tone:+.2f}"
    return ComponentScore(
        name="social_attention", score=round(score, 2), weight=weight, detail=detail
    )


def _score_breadth(read: market.ThemeMarketRead, weight: float) -> ComponentScore:
    basket = read.basket
    if not basket.symbols_with_data or basket.breadth_pct is None:
        return ComponentScore(
            name="breadth",
            score=0.0,
            weight=weight,
            covered=False,
            detail="not enough constituents with price history",
        )
    score = basket.breadth_pct
    if basket.concentration_pct is not None and basket.concentration_pct > 50:
        # One name accounting for most of the theme's gain is not breadth.
        score *= 0.6
    if basket.symbols_with_data < 4:
        score *= 0.7  # a "theme" of three names is a watchlist
    return ComponentScore(
        name="breadth",
        score=round(_clamp(score), 2),
        weight=weight,
        detail=(
            f"{basket.breadth_pct:.0f}% of {basket.symbols_with_data} members above their "
            "50-day average"
            + (
                f"; top name is {basket.concentration_pct:.0f}% of the basket's gain"
                if basket.concentration_pct is not None
                else ""
            )
        ),
    )


# ------------------------------------------------------------ legitimacy ----
def assess_legitimacy(
    components: list[ComponentScore], read: market.ThemeMarketRead, raw_score: float
) -> tuple[Legitimacy, list[str], float]:
    """Classify the trend and return the cap its classification implies.

    The hype signature is specific and worth naming: attention without
    confirmation, a basket carried by one stock, and no policy or institutional
    footprint. The returned cap is a CEILING — this function can only reduce a
    score, never increase one.
    """
    by_name = {c.name: c for c in components}
    social = by_name.get("social_attention")
    news = by_name.get("news_momentum")
    market_c = by_name.get("market_confirmation")
    policy = by_name.get("policy_support")
    etf = by_name.get("etf_activity")
    breadth = by_name.get("breadth")

    flags: list[str] = []

    talk = max(social.score if social else 0.0, news.score if news else 0.0)
    action = max(
        market_c.score if market_c and market_c.covered else 0.0,
        policy.score if policy and policy.covered else 0.0,
        etf.score if etf and etf.covered else 0.0,
    )

    if social and social.score >= 70 and (not market_c or market_c.score < 50):
        flags.append("loud on social media while the basket is not outperforming")
    if talk >= 60 and action < 35:
        flags.append("heavy coverage with little market, policy or ETF confirmation")
    if read.basket.concentration_pct is not None and read.basket.concentration_pct > 65:
        flags.append(
            f"one stock accounts for {read.basket.concentration_pct:.0f}% of the basket's gain"
        )
    if breadth and breadth.covered and breadth.score < 30:
        flags.append("few constituents participating — narrow move")
    if read.basket.symbols_with_data and read.basket.symbols_with_data < 4:
        flags.append("too few constituents with data to call it a sector move")

    measured = [c for c in components if c.covered]
    if len(measured) < 3:
        return "unproven", flags, 45.0

    confirmations = sum(
        1
        for c in (market_c, policy, etf)
        if c is not None and c.covered and c.score >= 55
    )

    if len(flags) >= 2 and confirmations == 0:
        return "hype", flags, 35.0
    if flags and confirmations <= 1:
        return "mixed", flags, 60.0
    if confirmations >= 2 and raw_score >= 55:
        return "legitimate", flags, 100.0
    if confirmations >= 1:
        return "emerging", flags, 85.0
    return "unproven", flags, 55.0


def persistence_bonus(db: Session, theme_key: str, today: date, raw_score: float) -> float:
    """Small bonus for a score that has held up over recent days.

    A trend that scored 70 for a week is more real than one that hit 70 this
    morning. Capped at 5 points so it can never manufacture a trend on its own.
    """
    since = today - timedelta(days=PERSISTENCE_WINDOW_DAYS)
    scores = db.execute(
        select(TrendSnapshotRow.score).where(
            TrendSnapshotRow.theme == theme_key,
            TrendSnapshotRow.day >= since,
            TrendSnapshotRow.day < today,
        )
    ).scalars().all()
    if len(scores) < 3:
        return 0.0
    sustained = len([s for s in scores if s >= 50])
    return round(min(5.0, sustained / len(scores) * 5.0), 2)


# ---------------------------------------------------------------- driver ----
def theme_symbols(db: Session, theme: Theme, corpus: ThemeCorpus, limit: int = 40) -> list[str]:
    """The working constituent list for a theme.

    Seeds ∪ ETF holdings ∪ tickers extracted from this theme's own news. The
    third term is what lets a company that just won a contract be considered
    even though nobody put it on a list beforehand.
    """
    from sentinel.data.universe import get_universe

    tradeable = {s.upper() for s in get_universe(db)}
    from sentinel.trends.taxonomy import thematic_etfs as _etfs

    etf_set = {e.upper() for e in _etfs()}

    members = {s.upper() for s in theme.seeds}
    members |= etf_members(db, list(theme.etfs))
    mentions = corpus.symbol_mentions.get(theme.key, {})
    members |= set(mentions)

    # ETFs are tracked for their flow proxy; they are never stock picks.
    members -= etf_set

    ranked = sorted(members, key=lambda s: (-mentions.get(s, 0), s))
    known = [s for s in ranked if s in tradeable or s in {x.upper() for x in theme.seeds}]
    return known[:limit]


def score_theme(
    db: Session,
    theme: Theme,
    corpus: ThemeCorpus,
    series: market.Series,
    benchmark_closes: list[float],
    today: date | None = None,
    gov_covered: bool = True,
    social_covered: bool = True,
) -> TrendScore:
    """Full assessment of one theme. Deterministic; no LLM."""
    day = today or date.today()
    weights = _weights_for(theme)
    symbols = theme_symbols(db, theme, corpus)
    read = market.read_theme(series, symbols, list(theme.etfs), benchmark_closes)
    accumulation = etf_accumulation(db, list(theme.etfs))
    growth = social_growth(db, set(symbols), now=day)

    components = [
        _score_news(theme, corpus, weights["news_momentum"]),
        _score_policy(theme, corpus, weights["policy_support"], gov_covered),
        _score_market(read, weights["market_confirmation"]),
        _score_etf(read, accumulation, weights["etf_activity"]),
        _score_social(theme, corpus, growth, weights["social_attention"], social_covered),
        _score_breadth(read, weights["breadth"]),
    ]

    # Uncovered components are excluded from BOTH numerator and denominator,
    # so an unreachable source lowers confidence rather than silently scoring
    # the theme as zero on that axis.
    covered = [c for c in components if c.covered]
    total_weight = sum(c.weight for c in covered)
    raw = (
        sum(c.contribution for c in covered) / total_weight * 100.0 if total_weight > 0 else 0.0
    )
    raw += persistence_bonus(db, theme.key, day, raw)
    raw = _clamp(raw)

    legitimacy, flags, cap = assess_legitimacy(components, read, raw)
    final = _clamp(min(raw, cap))

    return TrendScore(
        theme=theme.key,
        name=theme.name,
        day=day,
        score=round(final, 2),
        raw_score=round(raw, 2),
        legitimacy=legitimacy,
        components=components,
        hype_flags=flags,
        symbols=symbols,
        market_read=read,
        evidence={
            "headlines": corpus.headlines.get(theme.key, [])[:6],
            "government": corpus.gov_headlines.get(theme.key, [])[:5],
            "etf_accumulation": [a.model_dump() for a in accumulation[:10]],
            "etfs_accumulating": read.etfs_accumulating,
            "social": growth,
            # Per-symbol theme-news mention counts, carried so the ranking
            # stage can explain a name's connection without re-querying.
            "symbol_mentions": {
                s: n for s, n in corpus.symbol_mentions.get(theme.key, {}).items() if s in symbols
            },
            "coverage_gaps": [c.name for c in components if not c.covered],
            "capped_by_legitimacy": round(raw - final, 2) if final < raw else 0.0,
        },
        explanation=build_explanation(theme, components, read, legitimacy, flags),
    )


def build_explanation(
    theme: Theme,
    components: list[ComponentScore],
    read: market.ThemeMarketRead,
    legitimacy: Legitimacy,
    flags: list[str],
) -> str:
    """Plain-English "why this trend is growing", assembled from the same
    sub-scores that produced the number. No LLM — the components already
    contain the reasoning, they just need joining up."""
    ranked = sorted(
        [c for c in components if c.covered and c.score > 0], key=lambda c: -c.contribution
    )
    if not ranked:
        return f"No measurable activity for {theme.name.lower()} in the current window."

    lead = ranked[0]
    labels = {
        "news_momentum": "news coverage is accelerating",
        "policy_support": "government and regulatory activity is picking up",
        "market_confirmation": "the constituent basket is outperforming the market",
        "etf_activity": "thematic ETFs are strengthening on rising turnover",
        "social_attention": "investor discussion is growing",
        "breadth": "the move is broad across constituents",
    }
    parts = [f"{theme.description} Right now, {labels.get(lead.name, lead.name)}: {lead.detail}."]
    for component in ranked[1:3]:
        parts.append(f"{labels.get(component.name, component.name).capitalize()} — {component.detail}.")

    if legitimacy == "hype":
        parts.append(
            "This currently looks more like hype than a durable trend: "
            + "; ".join(flags)
            + ". Treat it with scepticism."
        )
    elif legitimacy == "mixed" and flags:
        parts.append("Caveats: " + "; ".join(flags) + ".")
    elif legitimacy == "legitimate":
        parts.append(
            "Multiple independent confirmations (market, policy and/or ETF flows) "
            "point the same way, which is what separates a trend from a story."
        )
    gaps = [c.name for c in components if not c.covered]
    if gaps:
        parts.append(
            "Not measured today (source unavailable): " + ", ".join(gaps) + "."
        )
    return " ".join(parts)


def score_all(db: Session, today: date | None = None, persist: bool = True) -> list[TrendScore]:
    """Score every theme, ranked strongest first, and persist the snapshots."""
    day = today or date.today()
    corpus = build_corpus(db)

    gov_covered = any(corpus.recent_gov.values()) or any(corpus.baseline_gov.values())
    social_covered = any(corpus.recent_social.values()) or any(corpus.baseline_social.values())

    # One bar query for everything: all seeds, all tracked ETFs, the benchmark.
    from sentinel.trends.taxonomy import seed_symbols, thematic_etfs

    wanted = {market.BENCHMARK} | set(seed_symbols()) | set(thematic_etfs())
    for theme in THEMES:
        wanted |= set(corpus.symbol_mentions.get(theme.key, {}))
    series = market.load_series(db, wanted)
    benchmark_closes = [c for c, _ in series.get(market.BENCHMARK, [])]

    scores: list[TrendScore] = []
    for theme in THEMES:
        try:
            scores.append(
                score_theme(
                    db,
                    theme,
                    corpus,
                    series,
                    benchmark_closes,
                    today=day,
                    gov_covered=gov_covered,
                    social_covered=social_covered,
                )
            )
        except Exception:
            log.exception("theme scoring failed", theme=theme.key)

    scores.sort(key=lambda s: -s.score)
    if persist:
        for score in scores:
            persist_score(db, score)
        db.flush()
    return scores


def persist_score(db: Session, score: TrendScore) -> None:
    """Upsert one theme's snapshot for its day.

    Public because the report stage re-persists after an LLM review may have
    lowered a score — the stored snapshot should reflect what the user was
    actually shown.
    """
    components = {
        c.name: {
            "score": c.score,
            "weight": c.weight,
            "detail": c.detail,
            "covered": c.covered,
        }
        for c in score.components
    }
    evidence = {
        **score.evidence,
        "raw_score": score.raw_score,
        "explanation": score.explanation,
        "hype_flags": score.hype_flags,
    }

    existing = db.get(TrendSnapshotRow, (score.theme, score.day))
    if existing is None:
        db.add(
            TrendSnapshotRow(
                theme=score.theme,
                day=score.day,
                score=score.score,
                legitimacy=score.legitimacy,
                components=components,
                evidence=evidence,
                symbols=list(score.symbols),
            )
        )
    else:
        existing.score = score.score
        existing.legitimacy = score.legitimacy
        existing.components = components
        existing.evidence = evidence
        existing.symbols = list(score.symbols)
        existing.computed_at = datetime.now(UTC)


def latest_snapshots(db: Session, limit: int = 20) -> list[TrendSnapshotRow]:
    """Most recent day's snapshots, strongest first."""
    day = db.execute(
        select(TrendSnapshotRow.day).order_by(TrendSnapshotRow.day.desc()).limit(1)
    ).scalar_one_or_none()
    if day is None:
        return []
    return list(
        db.execute(
            select(TrendSnapshotRow)
            .where(TrendSnapshotRow.day == day)
            .order_by(TrendSnapshotRow.score.desc())
            .limit(limit)
        ).scalars().all()
    )


def symbol_theme_alignment(db: Session, min_score: float = 50.0) -> dict[str, list[tuple[str, float]]]:
    """Symbol → [(theme, score)] for the latest snapshots above `min_score`.

    Consumed by the discovery engine's `trend_alignment` trigger, which is how
    a theme's constituents enter the normal scan set and therefore the normal
    analysis → risk → portfolio path.
    """
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for snapshot in latest_snapshots(db, limit=len(THEMES)):
        if snapshot.score < min_score:
            continue
        for symbol in snapshot.symbols or []:
            out[str(symbol).upper()].append((snapshot.theme, float(snapshot.score)))
    return dict(out)
