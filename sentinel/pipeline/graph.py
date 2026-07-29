"""LangGraph agent pipeline (spec §4): a typed StateGraph over PipelineState.

Multi-stage filtering funnel — everything cheap runs first, and the LLM runs
last on whatever tiny set survives:

    Universe (700+ names)
      ↓  load_context      DB only, no provider calls
      ↓  regime            deterministic
      ↓  screen            technical + liquidity + fundamental/quality filters
      ↓  analysts          deterministic fact tables (no LLM)
      ↓  select_strategy   deterministic rules
      ↓  sizing            fixed-fractional, pure arithmetic
      ↓  risk_prefilter    the real risk engine, run BEFORE any spend
      ↓  portfolio_review  deterministic position management
      ↓  llm_stage         event-gated, hard-capped, cached  ← the only cost
      ↓  synthesize        deterministic numbers + the review's prose
      ↓  risk_gate         FINAL AUTHORITY — every signal, no override

The risk engine appears twice on purpose. `risk_prefilter` is an economic
gate: it guarantees the model is never shown a candidate the engine would
reject, so tokens are never spent on a trade that cannot happen. `risk_gate`
is the authoritative one — it runs on EVERY signal after all interpretation,
exactly as before, and nothing can bypass it. The engine is a pure function,
so evaluating twice costs nothing and cannot disagree with itself.
"""

from functools import partial

import pandas as pd
import structlog
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from sentinel.agents.analysts import all_analysts
from sentinel.agents.regime import classify_regime
from sentinel.agents.review import build_fact_pack, review_candidate
from sentinel.agents.screener import ScreenerParams, screen
from sentinel.agents.technicals import compute_technicals
from sentinel.data.context import build_market_context
from sentinel.data.discovery import insider_net_shares
from sentinel.data.market_hours import trading_days_until
from sentinel.data.universe import held_symbols
from sentinel.db.models import SystemEvent
from sentinel.modes import policy_for
from sentinel.pipeline.state import CandidateState, PipelineState, Signal
from sentinel.pipeline.synthesizer import (
    compute_conviction,
    compute_risk_score,
    synthesize_signal,
)
from sentinel.pipeline.triggers import (
    discovery_events_by_symbol,
    evaluate_trigger,
    rank_and_cap,
)
from sentinel.portfolio.manager import PositionReview, review_positions
from sentinel.portfolio.sizing import size_position
from sentinel.portfolio.state import build_portfolio_state, compute_correlations
from sentinel.risk.engine import CandidateOrder, PortfolioState
from sentinel.risk.engine import evaluate as risk_evaluate
from sentinel.risk.store import get_active_profile
from sentinel.strategies.selector import select_strategy

log = structlog.get_logger()

# Deterministic confidence attached to position-management SELLs. These are
# code constants keyed off the review action, never model output.
_SELL_CONFIDENCE: dict[str, float] = {
    "EXIT": 0.90,
    "REDUCE": 0.85,
    "TAKE_PARTIAL_PROFITS": 0.80,
}


def _load_context(state: PipelineState, db: Session) -> dict:
    context = build_market_context(db, state.symbols)
    return {"context": context, "funnel": {**state.funnel, "universe": len(state.symbols)}}


def _regime_node(state: PipelineState, db: Session) -> dict:
    assert state.context is not None
    regime = classify_regime(state.context.spy_bars, state.context.macro.get("VIXCLS"))
    return {"regime": regime}


def _screen_node(state: PipelineState, db: Session) -> dict:
    """Widest deterministic stage: quality/liquidity/technical filters over
    every symbol in the run. Costs CPU only, so the universe can be large."""
    assert state.context is not None
    candidates: dict[str, CandidateState] = {}
    snapshots = {
        symbol: compute_technicals(symbol, sym_ctx.daily_bars)
        for symbol, sym_ctx in state.context.symbols.items()
    }
    results = screen(state.context, snapshots, ScreenerParams())
    for result in results:
        candidates[result.symbol] = CandidateState(
            symbol=result.symbol, snapshot=snapshots.get(result.symbol), screen=result
        )
    eligible = sum(1 for r in results if r.eligible)
    return {
        "candidates": candidates,
        "funnel": {**state.funnel, "screened": eligible},
    }


def _analysts_node(state: PipelineState, db: Session) -> dict:
    """Assemble the analyst fact tables.

    Deterministic by default — this is per-candidate work, so it is exactly
    where a naive design bleeds tokens. The LLM fan-out happens later, only
    for finalists, and only at 'full' depth.
    """
    assert state.context is not None
    candidates = dict(state.candidates)
    for symbol, cand in candidates.items():
        if not (cand.screen and cand.screen.eligible and cand.snapshot):
            continue
        cand.verdicts = all_analysts(
            db,
            state.context,
            symbol,
            cand.snapshot,
            use_llm=False,
            insider_net_shares_90d=insider_net_shares(db, symbol),
        )
    return {"candidates": candidates}


def _select_node(state: PipelineState, db: Session) -> dict:
    assert state.regime is not None
    candidates = dict(state.candidates)
    for cand in candidates.values():
        if not (cand.screen and cand.screen.eligible and cand.snapshot and cand.verdicts):
            continue
        cand.selection = select_strategy(
            db, cand.snapshot, cand.screen, cand.verdicts, state.regime, use_llm=False
        )
        cand.conviction = compute_conviction(cand.verdicts, cand.selection.fit.score)
    return {"candidates": candidates}


def _sizing_node(state: PipelineState, db: Session) -> dict:
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    candidates = dict(state.candidates)
    sized = 0
    for cand in candidates.values():
        if not (cand.selection and cand.selection.fit.action == "BUY" and cand.snapshot):
            continue
        if cand.snapshot.atr14 is None or cand.snapshot.close <= 0:
            continue
        cand.sizing = size_position(
            equity=portfolio.equity,
            entry_price=cand.snapshot.close,
            atr14=cand.snapshot.atr14,
            profile=profile,
        )
        if cand.sizing is not None:
            sized += 1
    return {"candidates": candidates, "funnel": {**state.funnel, "sized": sized}}


def _candidate_order(
    state: PipelineState,
    db: Session,
    symbol: str,
    action: str,
    shares: int,
    entry_price: float,
    portfolio: PortfolioState,
) -> CandidateOrder:
    """Build the risk engine's input for one candidate. Shared by the
    pre-filter and the terminal gate so the two can never drift apart."""
    cand = state.candidates.get(symbol)
    snap = cand.snapshot if cand else None
    sym_ctx = state.context.symbols.get(symbol) if state.context else None
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


def _risk_prefilter_node(state: PipelineState, db: Session) -> dict:
    """Run the real risk engine BEFORE any LLM spend.

    Purely economic: it stops tokens being spent interpreting a trade the
    engine would veto anyway. The terminal risk_gate still evaluates every
    signal and remains the only authority.
    """
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    candidates = dict(state.candidates)
    approved = 0
    for symbol, cand in candidates.items():
        if not (cand.selection and cand.snapshot):
            continue
        action = cand.selection.fit.action
        if action != "BUY" or cand.sizing is None:
            continue
        cand.risk_pre = risk_evaluate(
            _candidate_order(
                state,
                db,
                symbol,
                action,
                cand.sizing.shares,
                float(cand.sizing.max_entry_price),
                portfolio,
            ),
            portfolio,
            profile,
        )
        if cand.risk_pre.approved:
            approved += 1
    return {
        "candidates": candidates,
        "funnel": {**state.funnel, "risk_approved": approved},
    }


def _portfolio_review_node(state: PipelineState, db: Session) -> dict:
    """Deterministic position management. Runs in every mode including Free."""
    snapshots = {
        symbol: cand.snapshot
        for symbol, cand in state.candidates.items()
        if cand.snapshot is not None
    }
    earnings_days: dict[str, int] = {}
    if state.context is not None:
        for symbol, sym_ctx in state.context.symbols.items():
            if sym_ctx.next_earnings is not None:
                earnings_days[symbol] = trading_days_until(
                    pd.Timestamp(sym_ctx.next_earnings.date)
                )
    reviews = review_positions(
        db, snapshots=snapshots, regime=state.regime, earnings_days=earnings_days
    )
    return {"position_reviews": reviews}


def _deep_analysis(state: PipelineState, db: Session, symbol: str) -> None:
    """Full multi-agent refinement for one finalist (research depth only).

    Re-runs the analysts with LLM interpretation, lets the selector's LLM
    tie-break vote, and re-sizes off the refined selection. Mutates the
    candidate in place.
    """
    assert state.context is not None and state.regime is not None
    cand = state.candidates[symbol]
    if cand.snapshot is None or cand.screen is None:
        return
    cand.verdicts = all_analysts(
        db,
        state.context,
        symbol,
        cand.snapshot,
        use_llm=True,
        insider_net_shares_90d=insider_net_shares(db, symbol),
    )
    cand.selection = select_strategy(
        db, cand.snapshot, cand.screen, cand.verdicts, state.regime, use_llm=True
    )
    cand.conviction = compute_conviction(cand.verdicts, cand.selection.fit.score)
    if cand.selection.fit.action == "BUY" and cand.snapshot.atr14:
        portfolio = build_portfolio_state(db)
        cand.sizing = size_position(
            equity=portfolio.equity,
            entry_price=cand.snapshot.close,
            atr14=cand.snapshot.atr14,
            profile=get_active_profile(db),
        )


def _llm_stage_node(state: PipelineState, db: Session) -> dict:
    """The only stage that can cost money.

    Eligibility is deliberately narrow: a candidate must have passed every
    deterministic filter AND the risk pre-filter, must clear the mode's
    conviction floor, and must have a MATERIAL EVENT behind it (earnings,
    filing, news, breakout, unusual volume, an open position, or a genuinely
    high-conviction setup). Survivors are ranked and truncated to the mode's
    hard per-scan cap, and each one costs a single cached call.
    """
    if state.depth == "none":
        return {"funnel": {**state.funnel, "llm_reviewed": 0}}

    policy = policy_for(state.mode)
    events = discovery_events_by_symbol(db)
    held = set(held_symbols(db))

    triggers = []
    for symbol, cand in state.candidates.items():
        if cand.selection is None or cand.snapshot is None:
            continue
        if cand.selection.fit.action not in ("BUY", "SELL"):
            continue
        if cand.risk_pre is None or not cand.risk_pre.approved:
            continue
        trigger = evaluate_trigger(
            symbol=symbol,
            conviction=cand.conviction,
            events=events.get(symbol),
            pct_from_52w_high=cand.snapshot.pct_from_52w_high,
            relative_volume=cand.snapshot.relative_volume,
            is_open_position=symbol in held,
            user_requested=state.on_demand,
            min_conviction=policy.min_conviction_for_llm,
        )
        if trigger is not None:
            triggers.append(trigger)

    selected = rank_and_cap(triggers, policy.max_llm_candidates_per_scan)
    if not selected:
        log.info("no candidate earned an LLM call", mode=state.mode, considered=len(triggers))
        return {"funnel": {**state.funnel, "llm_reviewed": 0}}

    candidates = dict(state.candidates)
    calls = 0
    for trigger in selected:
        if state.depth == "full":
            # Research depth: the per-analyst fan-out and the selector's LLM
            # tie-break, but ONLY here — on a finalist, inside the cap. Mutates
            # the candidate in place.
            _deep_analysis(state, db, trigger.symbol)
        cand = candidates[trigger.symbol]
        if cand.snapshot is None or cand.screen is None or cand.selection is None:
            continue
        assert state.regime is not None
        facts = build_fact_pack(
            symbol=trigger.symbol,
            snap=cand.snapshot,
            screen=cand.screen,
            sym_ctx=state.context.symbols.get(trigger.symbol) if state.context else None,
            verdicts=cand.verdicts,
            regime=state.regime,
            strategy=cand.selection.fit.strategy,
            action=cand.selection.fit.action,
            sizing=cand.sizing,
            risk_check=cand.risk_pre,
            base_confidence=cand.conviction,
            macro=_macro_tail(state),
        )
        cand.review = review_candidate(db, trigger.symbol, facts, trigger=trigger.label)
        cand.llm_trigger = trigger.label
        if cand.review.llm_used:
            calls += 1
    db.add(
        SystemEvent(
            kind="pipeline.llm_stage",
            message=(
                f"{len(selected)} candidate(s) reviewed ({calls} paid call(s), "
                f"{len(selected) - calls} cached/degraded) in {state.mode} mode"
            ),
            payload={
                "run_id": str(state.run_id),
                "depth": state.depth,
                "reviewed": [t.symbol for t in selected],
                "triggers": {t.symbol: t.label for t in selected},
            },
        )
    )
    db.flush()
    return {
        "candidates": candidates,
        "llm_calls": state.llm_calls + calls,
        "funnel": {**state.funnel, "llm_reviewed": len(selected)},
    }


def _macro_tail(state: PipelineState) -> dict:
    """Last value of each macro series — enough context, negligible tokens."""
    if state.context is None:
        return {}
    out: dict[str, float] = {}
    for series, points in state.context.macro.items():
        live = [p for p in points if p.value is not None]
        if live:
            out[series] = float(live[-1].value or 0.0)
    return out


def _position_sell_signal(review: PositionReview, regime: str) -> Signal:
    """Turn a deterministic position-management verdict into a SELL signal so
    it flows through persistence, the risk gate, and alerting like any other."""
    from decimal import Decimal

    return Signal(
        ticker=review.symbol,
        action="SELL",
        shares=abs(review.shares_delta),
        max_entry_price=Decimal(str(round(review.mark, 4))),
        confidence=_SELL_CONFIDENCE.get(review.action, 0.75),
        risk_score=compute_risk_score(None, regime),
        time_horizon="position_weeks",
        strategy="position-management",
        regime=regime,
        evidence=[],
        explanation=(f"{review.action.replace('_', ' ').title()}: " + "; ".join(review.reasons))[
            :500
        ],
        deterministic_only=True,
    )


def _synthesize_node(state: PipelineState, db: Session) -> dict:
    assert state.regime is not None
    signals = [
        synthesize_signal(db, cand, state.regime, review=cand.review)
        for cand in state.candidates.values()
        if cand.selection is not None
    ]
    # Position-management exits become SELL signals so they inherit the same
    # persistence, risk gate and alert routing as everything else. Skipped when
    # the ticker already produced a signal this run (no duplicate rows).
    covered = {s.ticker for s in signals}
    for review in state.position_reviews:
        if review.is_sell and review.symbol not in covered:
            signals.append(_position_sell_signal(review, state.regime.regime))
    return {"signals": signals}


def _risk_gate_node(state: PipelineState, db: Session) -> dict:
    """Final gate: every signal gets a full RiskCheckResult; rejections are
    logged to the audit trail. No override path exists."""
    assert state.context is not None
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    signals = []
    for signal in state.signals:
        order = _candidate_order(
            state,
            db,
            signal.ticker,
            signal.action,
            signal.shares or 0,
            float(signal.max_entry_price or 0),
            portfolio,
        )
        signal.risk_check = risk_evaluate(order, portfolio, profile)
        if signal.action in ("BUY", "SELL") and not signal.risk_check.approved:
            failed = signal.risk_check.failed_rules()
            db.add(
                SystemEvent(
                    level="WARN",
                    kind="signal.rejected",
                    message=f"{signal.action} {signal.ticker} vetoed by risk engine",
                    payload={"signal_id": str(signal.id), "failed_rules": failed},
                )
            )
            log.warning("signal vetoed", ticker=signal.ticker, failed_rules=failed)
        signals.append(signal)
    db.flush()
    actionable = sum(1 for s in signals if s.actionable)
    return {"signals": signals, "funnel": {**state.funnel, "actionable": actionable}}


_NODES = [
    ("load_context", _load_context),
    ("regime", _regime_node),
    ("screen", _screen_node),
    ("analysts", _analysts_node),
    ("select_strategy", _select_node),
    ("sizing", _sizing_node),
    ("risk_prefilter", _risk_prefilter_node),
    ("portfolio_review", _portfolio_review_node),
    ("llm_stage", _llm_stage_node),
    ("synthesize", _synthesize_node),
    ("risk_gate", _risk_gate_node),
]


def build_graph(db: Session):
    graph = StateGraph(PipelineState)
    previous = START
    for name, fn in _NODES:
        graph.add_node(name, partial(fn, db=db))
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph.compile()
