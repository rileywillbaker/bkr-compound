"""Swing pipeline runner — the swing book's equivalent of pipeline/runner.py.

It mirrors the core graph's sequence (context → regime → technicals → swing
screen → deterministic analysts → sizing → risk pre-filter → capped LLM
review → synthesize → risk gate) but reuses every deterministic building
block and, critically, terminates in the IDENTICAL risk engine
(`sentinel.risk.engine.evaluate`) with no override path. Signals are tagged
book="swing" and persisted through the shared save_signals, so the long-term
pipeline is never touched.

Cost: the swing book runs TWICE a trading day, so a per-candidate LLM fan-out
here is the single most expensive thing the system could do. It doesn't do
that. Analyst fact tables are deterministic; at most SWING_MAX_REVIEWS
finalists — those that passed the screen, the strategy fit, sizing AND the
risk engine — get one cached combined review each, and only when the
operating mode allows any spend at all.

Numeric trade parameters come only from `size_position` + market data; the
LLM writes prose (the explanation) and an advisory stance that can only stand
a trade down, never up — never a number.
"""

import contextlib
from datetime import UTC, datetime
from uuid import uuid4

import pandas as pd
import structlog
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sentinel import DISCLAIMER
from sentinel.agents.analysts import all_analysts
from sentinel.agents.regime import classify_regime
from sentinel.agents.review import build_fact_pack, review_candidate
from sentinel.agents.technicals import compute_technicals
from sentinel.data.bus import publish
from sentinel.data.context import MarketContext, build_market_context
from sentinel.data.discovery import insider_net_shares
from sentinel.data.market_hours import trading_days_until
from sentinel.db.models import FundamentalsRow, SystemEvent
from sentinel.modes import get_policy
from sentinel.pipeline.persist import save_signals
from sentinel.pipeline.state import CandidateState, PipelineState, Signal
from sentinel.pipeline.synthesizer import compute_conviction, synthesize_signal
from sentinel.pipeline.triggers import discovery_events_by_symbol, evaluate_trigger, rank_and_cap
from sentinel.portfolio.sizing import size_position
from sentinel.portfolio.state import build_portfolio_state, compute_correlations
from sentinel.risk.engine import CandidateOrder, PortfolioState
from sentinel.risk.engine import evaluate as risk_evaluate
from sentinel.risk.profile import RiskProfile
from sentinel.risk.store import get_active_profile
from sentinel.swing.alerts import route_swing_alerts
from sentinel.swing.context import build_screen_context
from sentinel.swing.screen import SwingScreenParams, swing_screen, swing_universe
from sentinel.swing.strategies import select_swing_strategy

log = structlog.get_logger()

BOOK = "swing"

# The swing book scans twice a day. Its own cap sits under the core cap so a
# busy swing session can never starve — or outspend — the long-term book.
SWING_MAX_REVIEWS = 2

_last_run: "SwingScanResult | None" = None


class SwingScanResult(BaseModel):
    run_id: str
    generated_at: datetime
    regime: str | None
    universe_size: int
    scanned: int
    screened: list[str]  # eligible symbols that reached the analysts
    signals: list[Signal]
    alerts_sent: int = 0
    disclaimer: str = DISCLAIMER
    errors: list[str] = Field(default_factory=list)


def last_swing_run() -> "SwingScanResult | None":
    return _last_run


def _order_for(
    db: Session,
    symbol: str,
    action: str,
    shares: int,
    entry_price: float,
    context: MarketContext,
    candidates: dict[str, CandidateState],
    portfolio: PortfolioState,
) -> CandidateOrder:
    """Build the risk engine's input. Shared by the pre-filter and the gate so
    the two can never disagree about what was evaluated."""
    cand = candidates.get(symbol)
    snap = cand.snapshot if cand else None
    sym_ctx = context.symbols.get(symbol)
    earnings = sym_ctx.next_earnings if sym_ctx else None
    held = [p.symbol for p in portfolio.positions if p.shares != 0]
    return CandidateOrder(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        shares=shares,
        entry_price=entry_price,
        sector=sym_ctx.sector if sym_ctx else "",
        avg_dollar_volume=snap.avg_dollar_volume20 if snap else None,
        atr_pct=snap.atr_pct if snap else None,
        trading_days_to_earnings=(
            trading_days_until(pd.Timestamp(earnings.date)) if earnings else None
        ),
        correlations=(
            compute_correlations(db, symbol, held) if action == "BUY" and held else {}
        ),
    )


def _review_finalists(
    db: Session,
    candidates: dict[str, CandidateState],
    context: MarketContext,
    regime,
    portfolio: PortfolioState,
    profile: RiskProfile,
    use_llm: bool,
) -> int:
    """Risk pre-filter, then at most SWING_MAX_REVIEWS cached review calls.

    The pre-filter is the economic gate: a swing candidate the risk engine
    would veto is never worth interpreting, so it never reaches the model.
    Returns the number of paid calls actually made.
    """
    policy = get_policy(db)
    for symbol, cand in candidates.items():
        if cand.selection is None or cand.snapshot is None:
            continue
        cand.conviction = compute_conviction(cand.verdicts, cand.selection.fit.score)
        if cand.selection.fit.action != "BUY" or cand.sizing is None:
            continue
        cand.risk_pre = risk_evaluate(
            _order_for(
                db,
                symbol,
                "BUY",
                cand.sizing.shares,
                float(cand.sizing.max_entry_price),
                context,
                candidates,
                portfolio,
            ),
            portfolio,
            profile,
        )

    if not use_llm or policy.scan_depth == "none":
        return 0

    events = discovery_events_by_symbol(db)
    triggers = []
    for symbol, cand in candidates.items():
        if cand.risk_pre is None or not cand.risk_pre.approved or cand.snapshot is None:
            continue
        trigger = evaluate_trigger(
            symbol=symbol,
            conviction=cand.conviction,
            events=events.get(symbol),
            pct_from_52w_high=cand.snapshot.pct_from_52w_high,
            relative_volume=cand.snapshot.relative_volume,
            min_conviction=policy.min_conviction_for_llm,
        )
        if trigger is not None:
            triggers.append(trigger)

    calls = 0
    cap = min(SWING_MAX_REVIEWS, policy.max_llm_candidates_per_scan)
    for trigger in rank_and_cap(triggers, cap):
        cand = candidates[trigger.symbol]
        if cand.snapshot is None or cand.screen is None or cand.selection is None:
            continue
        facts = build_fact_pack(
            symbol=trigger.symbol,
            snap=cand.snapshot,
            screen=cand.screen,
            sym_ctx=context.symbols.get(trigger.symbol),
            verdicts=cand.verdicts,
            regime=regime,
            strategy=cand.selection.fit.strategy,
            action=cand.selection.fit.action,
            sizing=cand.sizing,
            risk_check=cand.risk_pre,
            base_confidence=cand.conviction,
        )
        cand.review = review_candidate(db, trigger.symbol, facts, trigger=trigger.label)
        cand.llm_trigger = trigger.label
        if cand.review.llm_used:
            calls += 1
    return calls


def _apply_risk_gate(
    db: Session,
    signals: list[Signal],
    context: MarketContext,
    candidates: dict[str, CandidateState],
    portfolio: PortfolioState,
    profile: RiskProfile,
) -> None:
    """The SAME gate as the core pipeline (graph._risk_gate_node): every
    signal gets a full RiskCheckResult from the pure-Python engine; there is
    no path around it. Vetoes are logged to the audit trail."""
    for signal in signals:
        order = _order_for(
            db,
            signal.ticker,
            signal.action,
            signal.shares or 0,
            float(signal.max_entry_price or 0),
            context,
            candidates,
            portfolio,
        )
        signal.risk_check = risk_evaluate(order, portfolio, profile)
        if signal.action in ("BUY", "SELL") and not signal.risk_check.approved:
            failed = signal.risk_check.failed_rules()
            db.add(
                SystemEvent(
                    level="WARN",
                    kind="signal.rejected",
                    message=f"swing {signal.action} {signal.ticker} vetoed by risk engine",
                    payload={
                        "signal_id": str(signal.id),
                        "book": BOOK,
                        "failed_rules": failed,
                    },
                )
            )
            log.warning("swing signal vetoed", ticker=signal.ticker, failed_rules=failed)
    db.flush()


def _ensure_fundamentals(db: Session, symbols: list[str]) -> None:
    """Top up fundamentals + quotes for the capped candidate set.

    The screen itself now needs a known sector and market cap, so a name with
    no fundamentals row never gets this far — what this fills is the *next*
    scan (coverage accumulates) and today's marks via fresh quotes. Scoped to
    the ≤max_candidates survivors, and the fundamentals call is TTL-cached, so
    it is rate-limit friendly. Guarded: a provider outage degrades gracefully
    (missing data → that candidate is vetoed and logged, never a crash)."""
    missing = [s for s in symbols if db.get(FundamentalsRow, s) is None]
    if not missing:
        return
    try:
        from sentinel.data import ingest

        ingest.ingest_fundamentals(db, symbols=missing)
        ingest.ingest_quotes(db, symbols=missing)
        db.flush()
    except Exception:
        log.warning("swing fundamentals enrichment failed", symbols=missing)


def run_swing_scan(
    db: Session,
    symbols: list[str] | None = None,
    use_llm: bool = True,
    send_alerts: bool = False,
    params: SwingScreenParams | None = None,
    enrich: bool = True,
) -> SwingScanResult:
    """Run the swing pipeline over the S&P 500 universe (or `symbols`).

    Two phases: a bars-only screen over the whole universe, then full analysis
    of the capped survivors. Persists its signals tagged book="swing" and, when
    `send_alerts` is set (the scheduled scans), routes qualifying ones to the
    swing alert channel. Manual/API runs default to no alerts, exactly like the
    core manual run."""
    global _last_run
    run_id = uuid4()
    universe = symbols or swing_universe(db)

    # Phase 1 — screen on a lightweight (bars-only) context over the universe.
    screen_ctx = build_screen_context(db, universe)
    regime = classify_regime(screen_ctx.spy_bars, screen_ctx.macro.get("VIXCLS"))
    technicals = {
        sym: compute_technicals(sym, sym_ctx.daily_bars)
        for sym, sym_ctx in screen_ctx.symbols.items()
    }
    screened, scanned = swing_screen(screen_ctx, technicals, params)
    screened_syms = [c.symbol for c in screened]

    # Phase 2 — full context (news/fundamentals) for just the survivors.
    if enrich:
        _ensure_fundamentals(db, screened_syms)
    context = build_market_context(db, screened_syms) if screened_syms else screen_ctx

    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    candidates: dict[str, CandidateState] = {}
    for cand_screen in screened:
        symbol = cand_screen.symbol
        snap = technicals[symbol]
        # Deterministic fact tables only — this loop runs over every screened
        # name, twice a day, and is exactly where an LLM fan-out would bleed.
        verdicts = all_analysts(
            db,
            context,
            symbol,
            snap,
            use_llm=False,
            insider_net_shares_90d=insider_net_shares(db, symbol),
        )
        selection = select_swing_strategy(snap, cand_screen.screen, verdicts, regime.regime)
        sizing = None
        if (
            selection.fit.action == "BUY"
            and snap.atr14 is not None
            and snap.close > 0
        ):
            sizing = size_position(
                equity=portfolio.equity,
                entry_price=snap.close,
                atr14=snap.atr14,
                profile=profile,
            )
        candidates[symbol] = CandidateState(
            symbol=symbol,
            snapshot=snap,
            screen=cand_screen.screen,
            verdicts=verdicts,
            selection=selection,
            sizing=sizing,
        )

    llm_calls = _review_finalists(
        db, candidates, context, regime, portfolio, profile, use_llm
    )

    signals = [
        synthesize_signal(db, cand, regime, review=cand.review)
        for cand in candidates.values()
    ]
    for signal in signals:
        signal.book = BOOK

    _apply_risk_gate(db, signals, context, candidates, portfolio, profile)

    state = PipelineState(
        run_id=run_id,
        symbols=universe,
        use_llm=use_llm,
        context=context,
        regime=regime,
        candidates=candidates,
        signals=signals,
    )
    save_signals(db, state)

    alerts_sent = route_swing_alerts(db, signals) if send_alerts else 0

    for signal in signals:
        with contextlib.suppress(Exception):
            publish(
                "swing_signal",
                {
                    "id": str(signal.id),
                    "ticker": signal.ticker,
                    "action": signal.action,
                    "confidence": signal.confidence,
                    "strategy": signal.strategy,
                    "regime": signal.regime,
                    "book": BOOK,
                    "approved": bool(signal.risk_check and signal.risk_check.approved),
                },
            )

    actionable = [s for s in signals if s.actionable]
    db.add(
        SystemEvent(
            kind="swing.run",
            message=f"swing scan of {scanned} symbols: {len(screened)} screened, "
            f"{len(signals)} signals, {len(actionable)} actionable, "
            f"{alerts_sent} alerted, {llm_calls} LLM call(s)",
            payload={
                "run_id": str(run_id),
                "regime": regime.regime,
                "universe_size": len(universe),
                "scanned": scanned,
                "screened": [c.symbol for c in screened],
                "llm_calls": llm_calls,
                "signals": [str(s.id) for s in signals],
            },
        )
    )
    db.flush()

    result = SwingScanResult(
        run_id=str(run_id),
        generated_at=datetime.now(UTC),
        regime=regime.regime,
        universe_size=len(universe),
        scanned=scanned,
        screened=[c.symbol for c in screened],
        signals=signals,
        alerts_sent=alerts_sent,
    )
    _last_run = result
    return result
