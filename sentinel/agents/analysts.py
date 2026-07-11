"""Analyst agents (spec §4.3): technicals, news/sentiment, fundamentals,
macro, options-flow stub.

Pattern: deterministic code assembles the fact table; the LLM only interprets
it into a verdict. If the LLM is unavailable (budget/keys/outage) each analyst
falls back to a deterministic rule-based verdict flagged deterministic_only.

Cost policy: every analyst runs on the cheap "triage" role (Haiku) — these
calls happen per candidate, so they dominate spend. The "reasoning" role
(Sonnet) is reserved for synthesis (see pipeline/synthesizer.py).
"""

import json
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from sentinel.agents.technicals import TechnicalSnapshot
from sentinel.agents.verdicts import AnalystVerdict, EvidenceItem, LLMVerdictPayload
from sentinel.data.context import MarketContext, SymbolContext
from sentinel.providers.llm.client import LLMError, complete_json

log = structlog.get_logger()


def _llm_or_fallback(
    db: Session,
    role: str,
    analyst: str,
    symbol: str,
    system: str,
    facts: dict,
    fallback: AnalystVerdict,
) -> AnalystVerdict:
    try:
        payload = complete_json(
            db,
            role=role,
            system=system,
            user=json.dumps(facts, default=str),
            schema=LLMVerdictPayload,
            endpoint=f"analyst.{analyst}",
        )
        return AnalystVerdict(
            analyst=analyst,
            symbol=symbol,
            score=payload.score,
            confidence=payload.confidence,
            summary=payload.summary,
            evidence=payload.evidence,
        )
    except LLMError as exc:
        log.warning("analyst falling back to deterministic", analyst=analyst, error=str(exc))
        return fallback


# ------------------------------------------------------------ technicals ----
_TECH_SYSTEM = (
    "You are a disciplined technical analyst. You receive a table of "
    "indicators computed in code — interpret them ONLY; never invent numbers. "
    "Score -100 (strong sell setup) to +100 (strong buy setup). Cite the "
    "specific indicator values you relied on as evidence items."
)


def deterministic_technicals_verdict(symbol: str, snap: TechnicalSnapshot) -> AnalystVerdict:
    score = 0.0
    evidence: list[EvidenceItem] = []

    def cite(datapoint: str):
        evidence.append(EvidenceItem(source="technicals", datapoint=datapoint))

    for flag, label, weight in (
        (snap.above_sma20, "SMA20", 10),
        (snap.above_sma50, "SMA50", 15),
        (snap.above_sma200, "SMA200", 20),
    ):
        if flag is True:
            score += weight
            cite(f"close above {label}")
        elif flag is False:
            score -= weight
            cite(f"close below {label}")
    if snap.rsi14 is not None:
        if snap.rsi14 >= 70:
            score -= 15
            cite(f"RSI14 overbought at {snap.rsi14:.1f}")
        elif snap.rsi14 <= 30:
            score += 10
            cite(f"RSI14 oversold at {snap.rsi14:.1f}")
        elif snap.rsi14 > 50:
            score += 10
            cite(f"RSI14 bullish at {snap.rsi14:.1f}")
        else:
            score -= 10
            cite(f"RSI14 bearish at {snap.rsi14:.1f}")
    if snap.macd_hist is not None:
        if snap.macd_hist > 0:
            score += 15
            cite(f"MACD histogram positive ({snap.macd_hist:.3f})")
        else:
            score -= 15
            cite(f"MACD histogram negative ({snap.macd_hist:.3f})")
    if snap.relative_volume is not None and snap.relative_volume >= 1.5:
        score += 10
        cite(f"relative volume {snap.relative_volume:.2f}x")
    bounded = int(max(-100, min(100, score)))
    return AnalystVerdict(
        analyst="technicals",
        symbol=symbol,
        score=bounded,
        confidence=0.5 if snap.bars_used >= 200 else 0.35,
        summary="rule-based technical read (LLM interpretation unavailable)",
        evidence=evidence[:10],
        deterministic_only=True,
    )


def technicals_analyst(
    db: Session, symbol: str, snap: TechnicalSnapshot, use_llm: bool = True
) -> AnalystVerdict:
    fallback = deterministic_technicals_verdict(symbol, snap)
    if not use_llm:
        return fallback
    facts = {"symbol": symbol, "indicators": snap.model_dump()}
    return _llm_or_fallback(
        db, "triage", "technicals", symbol, _TECH_SYSTEM, facts, fallback
    )


# ------------------------------------------------------------------ news ----
_NEWS_SYSTEM = (
    "You are a fast news-triage analyst. You receive recent headlines for one "
    "ticker plus market-wide items. Flag material events (earnings, guidance, "
    "M&A, litigation, regulatory, product). Score sentiment -100 (very "
    "negative) to +100 (very positive) for the ticker over a multi-day swing "
    "horizon. Cite headlines verbatim as evidence datapoints."
)


def news_analyst(
    db: Session, sym_ctx: SymbolContext, use_llm: bool = True
) -> AnalystVerdict:
    symbol = sym_ctx.symbol
    if not sym_ctx.news:
        return AnalystVerdict(
            analyst="news",
            symbol=symbol,
            score=0,
            confidence=0.1,
            summary="no recent news ingested",
            deterministic_only=True,
            unavailable=True,
        )
    fallback = AnalystVerdict(
        analyst="news",
        symbol=symbol,
        score=0,
        confidence=0.15,
        summary=f"{len(sym_ctx.news)} headlines present but LLM triage unavailable",
        deterministic_only=True,
    )
    if not use_llm:
        return fallback
    facts = {
        "symbol": symbol,
        "headlines": [
            {"headline": n.headline, "source": n.source, "published_at": n.published_at}
            for n in sym_ctx.news[:25]
        ],
    }
    return _llm_or_fallback(db, "triage", "news", symbol, _NEWS_SYSTEM, facts, fallback)


# ---------------------------------------------------------- fundamentals ----
_FUND_SYSTEM = (
    "You are a fundamentals analyst. You receive valuation metrics, growth "
    "rates, next earnings date, and recent insider filings for one ticker. "
    "Score the fundamental setup -100..+100 for a swing/position horizon. "
    "Cite the specific metrics you relied on."
)


def fundamentals_analyst(
    db: Session,
    sym_ctx: SymbolContext,
    insider_net_shares_90d: int | None = None,
    use_llm: bool = True,
) -> AnalystVerdict:
    symbol = sym_ctx.symbol
    have_data = any(v is not None for v in (sym_ctx.pe, sym_ctx.ps, sym_ctx.market_cap))
    if not have_data:
        return AnalystVerdict(
            analyst="fundamentals",
            symbol=symbol,
            score=0,
            confidence=0.1,
            summary="fundamentals unavailable (free-tier gap or not ingested)",
            deterministic_only=True,
            unavailable=True,
        )
    fallback = AnalystVerdict(
        analyst="fundamentals",
        symbol=symbol,
        score=0,
        confidence=0.2,
        summary="fundamentals present but LLM interpretation unavailable",
        deterministic_only=True,
    )
    if not use_llm:
        return fallback
    facts = {
        "symbol": symbol,
        "sector": sym_ctx.sector,
        "market_cap_millions": sym_ctx.market_cap,
        "pe_ttm": sym_ctx.pe,
        "ps_ttm": sym_ctx.ps,
        "beta": sym_ctx.beta,
        "pct_off_52w_high": None,
        "next_earnings": sym_ctx.next_earnings.model_dump() if sym_ctx.next_earnings else None,
        "insider_net_shares_90d": insider_net_shares_90d,
    }
    return _llm_or_fallback(
        db, "triage", "fundamentals", symbol, _FUND_SYSTEM, facts, fallback
    )


# ------------------------------------------------------------------ macro ----
_MACRO_SYSTEM = (
    "You are a macro analyst. You receive recent FRED series (VIX, fed funds "
    "rate, 10y-2y spread, CPI, unemployment) and a ticker's sector. Score how "
    "supportive the macro backdrop is for this sector, -100..+100. Cite the "
    "series values you relied on."
)


def macro_analyst(
    db: Session, context: MarketContext, sym_ctx: SymbolContext, use_llm: bool = True
) -> AnalystVerdict:
    symbol = sym_ctx.symbol
    if not context.macro:
        return AnalystVerdict(
            analyst="macro",
            symbol=symbol,
            score=0,
            confidence=0.1,
            summary="macro series not ingested",
            deterministic_only=True,
            unavailable=True,
        )
    fallback = AnalystVerdict(
        analyst="macro",
        symbol=symbol,
        score=0,
        confidence=0.2,
        summary="macro data present but LLM interpretation unavailable",
        deterministic_only=True,
    )
    if not use_llm:
        return fallback
    tail = {
        series: [
            {"date": p.date, "value": p.value} for p in points[-6:] if p.value is not None
        ]
        for series, points in context.macro.items()
    }
    facts = {"symbol": symbol, "sector": sym_ctx.sector, "series": tail}
    return _llm_or_fallback(db, "triage", "macro", symbol, _MACRO_SYSTEM, facts, fallback)


# ----------------------------------------------------------- options flow ----
def options_flow_analyst(db: Session, symbol: str) -> AnalystVerdict:
    """Phase 3 stub (spec §4.3): free-tier options data is inadequate.

    Paid upgrade path: a provider with options flow (e.g. Polygon.io options
    plans or CBOE data) implementing this same signature behind
    ResearchDataProvider-style config. Until then the factor is 'unavailable'
    and carries zero weight in synthesis.
    """
    return AnalystVerdict(
        analyst="options_flow",
        symbol=symbol,
        score=0,
        confidence=0.0,
        summary="options flow not implemented (free tier) — see docs for paid upgrade path",
        deterministic_only=True,
        unavailable=True,
    )


def all_analysts(
    db: Session,
    context: MarketContext,
    symbol: str,
    snap: TechnicalSnapshot,
    use_llm: bool = True,
    insider_net_shares_90d: int | None = None,
) -> list[AnalystVerdict]:
    sym_ctx = context.symbols[symbol]
    return [
        technicals_analyst(db, symbol, snap, use_llm=use_llm),
        news_analyst(db, sym_ctx, use_llm=use_llm),
        fundamentals_analyst(
            db, sym_ctx, insider_net_shares_90d=insider_net_shares_90d, use_llm=use_llm
        ),
        macro_analyst(db, context, sym_ctx, use_llm=use_llm),
        options_flow_analyst(db, symbol),
    ]


def utcnow() -> datetime:
    return datetime.now(UTC)
