"""The Trend Discovery Agent: orchestration and the hand-off to the other agents.

This is the module that makes the four agents a system rather than four
programs. It runs the funnel end to end and, critically, it does not decide
anything the other agents own:

    Trend Discovery      themes, strength, legitimacy, which companies are
    (scoring/ranking)    exposed and how good they are
            │
            ▼ candidate + reference price
    Stock Analysis       technical snapshot from stored bars; on request, the
    (agents/, pipeline/) full pipeline through the normal risk gate
            │
            ▼ proposed BUY
    Risk Management      size_position() then the pure-Python risk engine.
    (risk/, allocation)  ABSOLUTE VETO. A theme scoring 100 buys nothing if
                         the engine says no, and there is no override path.
            │
            ▼ approved dollar amount
    Portfolio Manager    current holdings, cash, sector concentration,
    (portfolio/)         correlation, and the open-position review
            │
            ▼
    The daily report

The funnel is also the cost control. Themes are scored deterministically for
every theme in the taxonomy; only the handful that clear a threshold are worth
an optional review call, and only names that survive the quality gate, the
pump guard AND the risk engine ever reach the report with a dollar amount
attached. Claude is the last step, and on most days it is not a step at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel.agents.technicals import TechnicalSnapshot, compute_technicals
from sentinel.data.context import build_market_context
from sentinel.db.models import SystemEvent
from sentinel.modes import LLMPolicy, get_policy
from sentinel.portfolio.manager import PositionReview, review_positions
from sentinel.risk.engine import PortfolioState, PositionState
from sentinel.trends import market, ranking, scoring
from sentinel.trends import review as trend_review
from sentinel.trends.allocation import Allocation, allocate, portfolio_context
from sentinel.trends.taxonomy import THEMES_BY_KEY

log = structlog.get_logger()

# --- funnel widths --------------------------------------------------------
MAX_THEMES_IN_REPORT = 5
MIN_THEME_SCORE = 45.0  # below this a theme is noise, not a trend
MAX_OPPORTUNITIES = 5  # dollar recommendations in one report
MAX_STOCKS_PER_THEME = 6
MIN_STOCK_CONFIDENCE = 0.45

# --- LLM caps per operating mode ------------------------------------------
# Free is structurally incapable of spending. Smart pays for at most two theme
# reviews a day; Research for four. These are per RUN, and the client's own
# daily dollar/call backstops sit underneath.
THEME_REVIEW_CAP: dict[str, int] = {"free": 0, "smart": 2, "research": 4}


class Opportunity(BaseModel):
    """One risk-approved, dollar-denominated recommendation."""

    symbol: str
    company: str = ""
    sector: str = ""
    price: float | None = None
    theme: str = ""
    theme_name: str = ""
    theme_score: float = 0.0
    theme_legitimacy: str = ""
    why_selected: str = ""
    trend_connection: str = ""
    bullish: list[str] = Field(default_factory=list)
    bearish: list[str] = Field(default_factory=list)
    risk_level: str = "Medium"
    confidence: float = 0.0
    factors: dict[str, float] = Field(default_factory=dict)
    allocation: Allocation | None = None

    @property
    def dollars(self) -> float:
        return self.allocation.dollars if self.allocation else 0.0

    @property
    def approved(self) -> bool:
        return bool(self.allocation and self.allocation.approved)


class TrendReport(BaseModel):
    """Everything the daily message, the API and the UI render from."""

    day: date
    generated_at: datetime
    market_environment: str = "Neutral"
    market_environment_detail: str = ""
    trends: list[scoring.TrendScore] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)
    rejected: list[Opportunity] = Field(default_factory=list)
    excluded_stocks: list[ranking.RankedStock] = Field(default_factory=list)
    position_reviews: list[PositionReview] = Field(default_factory=list)
    portfolio: dict = Field(default_factory=dict)
    llm_used: bool = False
    llm_calls: int = 0
    mode: str = "smart"
    coverage: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def actionable(self) -> list[Opportunity]:
        return [o for o in self.opportunities if o.approved]


def _snapshots(db: Session, symbols: list[str]) -> tuple[dict[str, TechnicalSnapshot], dict]:
    """Technical snapshots + next-earnings dates for a symbol list.

    This is the Stock Analysis agent's contribution at report depth: the same
    deterministic technicals the main pipeline computes, over the same stored
    bars. Running the *full* pipeline on every trend name would be the wrong
    trade — that is what `/api/pipeline/research` and the chat assistant are
    for, on demand, for a name the user actually cares about.
    """
    if not symbols:
        return {}, {}
    context = build_market_context(db, symbols)
    snaps: dict[str, TechnicalSnapshot] = {}
    earnings: dict[str, pd.Timestamp] = {}
    for symbol, sym_ctx in context.symbols.items():
        if sym_ctx.daily_bars:
            snaps[symbol] = compute_technicals(symbol, sym_ctx.daily_bars)
        if sym_ctx.next_earnings is not None:
            earnings[symbol] = pd.Timestamp(sym_ctx.next_earnings.date)
    return snaps, earnings


def _why_selected(stock: ranking.RankedStock, trend: scoring.TrendScore) -> str:
    """One sentence tying the company to the theme and to its own merits."""
    strongest = max(
        ranking.FACTOR_WEIGHTS,
        key=lambda name: getattr(stock.factors, name, 0.0),
    )
    labels = {
        "financial_health": "solid financial health",
        "revenue_growth": "strong revenue growth",
        "momentum": "market momentum",
        "institutional_interest": "institutional interest",
        "valuation": "reasonable valuation relative to its peers",
        "competitive_advantage": "a strong competitive position for its peer group",
    }
    lead = stock.bullish[0] if stock.bullish else labels.get(strongest, strongest)
    return (
        f"{trend.name} is scoring {trend.score:.0f}/100 ({trend.legitimacy}); "
        f"{stock.trend_connection.lower()}. Ranked top of that theme on "
        f"{labels.get(strongest, strongest)} — {lead}."
    )


def _review_themes(
    db: Session, trends: list[scoring.TrendScore], policy: LLMPolicy
) -> tuple[list[scoring.TrendScore], int, bool]:
    """Optionally apply the single per-theme LLM review, within the mode's cap.

    Returns (possibly-adjusted trends, live call count, any_llm_used). Cache
    hits cost nothing and are not counted against the cap.
    """
    cap = THEME_REVIEW_CAP.get(policy.mode, 0)
    if cap <= 0 or not policy.allows_any_llm:
        return trends, 0, False

    calls = 0
    used = False
    adjusted: list[scoring.TrendScore] = []
    for trend in trends:
        # Only spend on themes strong enough to actually appear in the report;
        # a theme scoring 20 needs no second opinion.
        if calls >= cap or trend.score < MIN_THEME_SCORE:
            adjusted.append(trend)
            continue
        review = trend_review.review_trend(db, trend)
        if review.llm_used:
            calls += 1
            used = True
        elif review.from_cache:
            used = True
        adjusted.append(trend_review.apply_review(trend, review))
    # A review can lower a score, so re-rank afterwards.
    adjusted.sort(key=lambda t: -t.score)
    return adjusted, calls, used


def generate_report(
    db: Session,
    today: date | None = None,
    use_llm: bool = True,
    persist: bool = True,
) -> TrendReport:
    """Run the whole funnel and build the day's trend report.

    Deterministic from end to end apart from the optional theme reviews, which
    are capped, cached, and one-directional.
    """
    day = today or date.today()
    policy = get_policy(db)
    environment, environment_detail = market.market_environment(db)

    report = TrendReport(
        day=day,
        generated_at=datetime.now(UTC),
        market_environment=environment,
        market_environment_detail=environment_detail,
        mode=policy.mode,
    )

    # --- 1. score every theme (deterministic, zero cost) -------------------
    trends = scoring.score_all(db, today=day, persist=persist)
    if not trends:
        report.notes.append("no themes could be scored — has collection run yet?")
        return report

    # --- 2. optional per-theme review (capped, cached, one-directional) ----
    if use_llm:
        trends, calls, used = _review_themes(db, trends, policy)
        report.llm_calls = calls
        report.llm_used = used
        if persist:
            for trend in trends:
                scoring.persist_score(db, trend)
            db.flush()
    else:
        report.notes.append("AI review skipped for this run")

    report.trends = trends[:MAX_THEMES_IN_REPORT]
    report.coverage = _coverage(trends)

    strong = [t for t in trends if t.score >= MIN_THEME_SCORE][:MAX_THEMES_IN_REPORT]
    if not strong:
        report.notes.append(
            f"no theme scored above {MIN_THEME_SCORE:.0f}/100 today — nothing worth acting on"
        )

    # --- 3. rank the stocks inside each strong theme ----------------------
    wanted: set[str] = set()
    for trend in strong:
        wanted |= set(trend.symbols)
    series = market.load_series(db, wanted | {market.BENCHMARK})
    benchmark_closes = [c for c, _ in series.get(market.BENCHMARK, [])]
    snaps, earnings = _snapshots(db, sorted(wanted))

    best_by_symbol: dict[str, tuple[ranking.RankedStock, scoring.TrendScore]] = {}
    for trend in strong:
        theme = THEMES_BY_KEY.get(trend.theme)
        if theme is None:
            continue
        accumulation = [
            scoring.Accumulation.model_validate(a)
            for a in trend.evidence.get("etf_accumulation", [])
        ]
        ranked, excluded = ranking.rank_theme_stocks(
            db,
            theme,
            trend.symbols,
            series,
            benchmark_closes,
            snapshots=snaps,
            accumulation=accumulation,
            theme_mentions=trend.evidence.get("symbol_mentions", {}),
            limit=MAX_STOCKS_PER_THEME,
        )
        report.excluded_stocks.extend(excluded[:6])
        for stock in ranked:
            # A name in two themes is credited to the stronger one, so the
            # report never recommends the same company twice.
            current = best_by_symbol.get(stock.symbol)
            if current is None or trend.score > current[1].score:
                best_by_symbol[stock.symbol] = (stock, trend)

    # --- 4. risk engine + portfolio manager on the best names -------------
    candidates = sorted(
        best_by_symbol.values(),
        key=lambda pair: -(pair[0].composite * (pair[1].score / 100.0)),
    )

    from sentinel.portfolio.state import build_portfolio_state, cash_balance
    from sentinel.risk.store import get_active_profile

    portfolio_state = build_portfolio_state(db)
    profile = get_active_profile(db)
    # The basket accumulates: each candidate is evaluated against a portfolio
    # that already contains the recommendations above it. Without this, five
    # names could each pass in isolation and together breach the sector,
    # correlation, open-position and gross-exposure limits — the report is a
    # basket the user might act on in one sitting, not five separate ideas.
    running_cash = cash_balance(db)

    for stock, trend in candidates:
        if len(report.opportunities) >= MAX_OPPORTUNITIES:
            break
        if stock.confidence < MIN_STOCK_CONFIDENCE:
            continue
        allocation = allocate(
            db,
            stock.symbol,
            stock.price or 0.0,
            snaps.get(stock.symbol),
            stock.sector,
            portfolio=portfolio_state,
            profile=profile,
            next_earnings=earnings.get(stock.symbol),
            cash=running_cash,
        )
        opportunity = Opportunity(
            symbol=stock.symbol,
            company=stock.company,
            sector=stock.sector,
            price=stock.price,
            theme=trend.theme,
            theme_name=trend.name,
            theme_score=trend.score,
            theme_legitimacy=trend.legitimacy,
            why_selected=_why_selected(stock, trend),
            trend_connection=stock.trend_connection,
            bullish=stock.bullish,
            bearish=stock.bearish,
            risk_level=stock.risk_level,
            confidence=stock.confidence,
            factors=stock.factors.as_dict(),
            allocation=allocation,
        )
        if allocation.approved:
            report.opportunities.append(opportunity)
            portfolio_state, running_cash = _with_proposed(
                portfolio_state, running_cash, opportunity
            )
        else:
            report.rejected.append(opportunity)

    # --- 5. portfolio manager: holdings, exposure, open-position review ---
    report.portfolio = portfolio_context(db)
    try:
        report.position_reviews = review_positions(db, snapshots=snaps)
    except Exception:
        log.exception("position review failed inside trend report")

    if not report.opportunities and strong:
        report.notes.append(
            "strong themes were found but no name passed the quality gate, the "
            "pump filter and the risk engine at an actionable size"
        )

    if persist:
        _persist_report(db, report)
    log.info(
        "trend report generated",
        trends=len(report.trends),
        opportunities=len(report.opportunities),
        rejected=len(report.rejected),
        llm_calls=report.llm_calls,
        mode=report.mode,
    )
    return report


def _with_proposed(
    portfolio: PortfolioState, cash: float, opportunity: Opportunity
) -> tuple[PortfolioState, float]:
    """Fold an approved recommendation into the working portfolio state.

    Returns a NEW state — the real portfolio is never mutated, and nothing is
    persisted. This exists so the next candidate's risk check sees the basket
    as it would actually be if the user took every recommendation above it:
    the sector, correlation, open-position and gross-exposure rules then bind
    on the basket rather than on each idea in isolation.

    Equity is unchanged (cash converts to position value); only the cash
    available for the next allocation falls.
    """
    allocation = opportunity.allocation
    if allocation is None or not allocation.approved:
        return portfolio, cash

    dollars = allocation.dollars
    if dollars <= 0:
        return portfolio, cash

    positions = list(portfolio.positions)
    existing = portfolio.position_for(opportunity.symbol)
    value = dollars + (existing.market_value if existing is not None else 0.0)
    if existing is not None:
        positions = [p for p in positions if p.symbol != opportunity.symbol]

    # Held as one notional unit rather than a share count. Every risk rule this
    # synthetic state feeds is value-based (position, sector, correlated and
    # gross exposure) or presence-based (open-position count), so notional is
    # exact — and it stays exact for a fractional recommendation, which a
    # rounded share count could not represent without over- or understating it.
    positions.append(
        PositionState(
            symbol=opportunity.symbol,
            shares=1,
            price=value,
            sector=opportunity.sector,
        )
    )
    return (
        portfolio.model_copy(update={"positions": positions}),
        max(0.0, cash - dollars),
    )


def _coverage(trends: list[scoring.TrendScore]) -> dict:
    """Which components could actually be measured across all themes."""
    gaps: dict[str, int] = {}
    for trend in trends:
        for name in trend.coverage_gaps:
            gaps[name] = gaps.get(name, 0) + 1
    return {
        "themes_scored": len(trends),
        "components_unmeasured": gaps,
        "fully_measured_themes": len([t for t in trends if not t.coverage_gaps]),
    }


def _persist_report(db: Session, report: TrendReport) -> None:
    from sentinel.db.models import TrendReportRow

    payload = report.model_dump(mode="json")
    db.add(
        TrendReportRow(
            day=report.day,
            market_environment=report.market_environment,
            payload=payload,
            text="",  # filled in by report.py when the message is composed
            llm_used=report.llm_used,
        )
    )
    db.add(
        SystemEvent(
            kind="trends.report",
            message=(
                f"trend report: {len(report.trends)} themes, "
                f"{len(report.opportunities)} risk-approved opportunities, "
                f"{report.llm_calls} LLM call(s) in {report.mode} mode"
            ),
            payload={
                "themes": [
                    {"theme": t.theme, "score": t.score, "legitimacy": t.legitimacy}
                    for t in report.trends
                ],
                "opportunities": [
                    {"symbol": o.symbol, "dollars": o.dollars} for o in report.opportunities
                ],
                "llm_calls": report.llm_calls,
            },
        )
    )
    db.flush()


def latest_report(db: Session):
    """Most recent stored report row, or None."""
    from sqlalchemy import select

    from sentinel.db.models import TrendReportRow

    return db.execute(
        select(TrendReportRow).order_by(TrendReportRow.created_at.desc()).limit(1)
    ).scalars().first()
