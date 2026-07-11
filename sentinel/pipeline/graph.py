"""LangGraph agent pipeline (spec §4): a typed StateGraph over PipelineState.

Regime → Screener → Analysts → Strategy Selector → Sizing → Synthesizer →
Risk Gate. Deterministic nodes are plain Python; the LLM appears only inside
analysts, the selector tie-break, and the explanation — never in a number.
The risk gate is the existing pure-Python engine and runs on EVERY signal;
there is no path around it.
"""

from functools import partial

import pandas as pd
import structlog
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from sentinel.agents.analysts import all_analysts
from sentinel.agents.regime import classify_regime
from sentinel.agents.screener import ScreenerParams, screen
from sentinel.agents.technicals import compute_technicals
from sentinel.data.context import build_market_context
from sentinel.data.discovery import insider_net_shares
from sentinel.data.market_hours import trading_days_until
from sentinel.db.models import SystemEvent
from sentinel.pipeline.state import CandidateState, PipelineState
from sentinel.pipeline.synthesizer import synthesize_signal
from sentinel.portfolio.sizing import size_position
from sentinel.portfolio.state import build_portfolio_state, compute_correlations
from sentinel.risk.engine import CandidateOrder
from sentinel.risk.engine import evaluate as risk_evaluate
from sentinel.risk.store import get_active_profile
from sentinel.strategies.selector import select_strategy

log = structlog.get_logger()


def _load_context(state: PipelineState, db: Session) -> dict:
    context = build_market_context(db, state.symbols)
    return {"context": context}


def _regime_node(state: PipelineState, db: Session) -> dict:
    assert state.context is not None
    regime = classify_regime(state.context.spy_bars, state.context.macro.get("VIXCLS"))
    return {"regime": regime}


def _screen_node(state: PipelineState, db: Session) -> dict:
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
    return {"candidates": candidates}


def _analysts_node(state: PipelineState, db: Session) -> dict:
    assert state.context is not None
    candidates = dict(state.candidates)
    for symbol, cand in candidates.items():
        if not (cand.screen and cand.screen.eligible and cand.snapshot):
            continue
        # spec calls for per-candidate parallelism; sequential is equivalent
        # for correctness and keeps LLM budget/backoff behavior simple
        cand.verdicts = all_analysts(
            db,
            state.context,
            symbol,
            cand.snapshot,
            use_llm=state.use_llm,
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
            db, cand.snapshot, cand.screen, cand.verdicts, state.regime,
            use_llm=state.use_llm,
        )
    return {"candidates": candidates}


def _sizing_node(state: PipelineState, db: Session) -> dict:
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    candidates = dict(state.candidates)
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
    return {"candidates": candidates}


def _synthesize_node(state: PipelineState, db: Session) -> dict:
    assert state.regime is not None
    signals = [
        synthesize_signal(db, cand, state.regime, use_llm=state.use_llm)
        for cand in state.candidates.values()
        if cand.selection is not None
    ]
    return {"signals": signals}


def _risk_gate_node(state: PipelineState, db: Session) -> dict:
    """Final gate: every signal gets a full RiskCheckResult; rejections are
    logged to the audit trail. No override path exists."""
    assert state.context is not None
    portfolio = build_portfolio_state(db)
    profile = get_active_profile(db)
    held = [p.symbol for p in portfolio.positions if p.shares != 0]
    signals = []
    for signal in state.signals:
        cand = state.candidates.get(signal.ticker)
        snap = cand.snapshot if cand else None
        sym_ctx = state.context.symbols.get(signal.ticker)
        earnings = sym_ctx.next_earnings if sym_ctx else None
        order = CandidateOrder(
            symbol=signal.ticker,
            action=signal.action,
            shares=signal.shares or 0,
            entry_price=float(signal.max_entry_price or 0),
            sector=sym_ctx.sector if sym_ctx else "",
            avg_dollar_volume=snap.avg_dollar_volume20 if snap else None,
            atr_pct=snap.atr_pct if snap else None,
            trading_days_to_earnings=(
                trading_days_until(pd.Timestamp(earnings.date)) if earnings else None
            ),
            correlations=(
                compute_correlations(db, signal.ticker, held)
                if signal.action == "BUY" and held
                else {}
            ),
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
    return {"signals": signals}


_NODES = [
    ("load_context", _load_context),
    ("regime", _regime_node),
    ("screen", _screen_node),
    ("analysts", _analysts_node),
    ("select_strategy", _select_node),
    ("sizing", _sizing_node),
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
