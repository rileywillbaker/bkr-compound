"""Conversational assistant (spec §7.1).

The LLM chooses tools and words ONLY. Every tool is deterministic code:
on-demand ticker analysis runs the real pipeline (which ends in the risk
gate), so chat can never bypass the risk engine, and nothing here can send
alerts or execute anything. Numeric trade parameters in replies come verbatim
from stored signals, never from the model.
"""

import json
from typing import Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import ChatMessageRow, SignalRow
from sentinel.db.settings_store import get_watchlist
from sentinel.providers.llm.client import LLMError, complete_json

log = structlog.get_logger()

MAX_TOOL_CALLS = 4

ToolName = Literal[
    "latest_signals", "portfolio", "market_context", "performance", "analyze_ticker"
]

_SYSTEM = (
    "You are B-Quant's assistant: a disciplined, plain-spoken analyst for a "
    "single user. You have READ-ONLY tools; call one when you need data, then "
    "answer. Rules: never invent numbers — quote only tool output; trade "
    "parameters (shares/prices/stops) exist only inside signals produced by "
    "the deterministic pipeline and risk engine; you cannot execute trades, "
    "send alerts, or override a risk rejection, and you must say so if asked. "
    "If asked whether to buy/sell a specific ticker, call analyze_ticker and "
    "present its signal and risk check verbatim — it works for ANY ticker, "
    "not just the watchlist (the watchlist is only the user's highlighted "
    "names). When uncertain the answer is NO TRADE. Keep replies under 250 "
    "words.\n\n"
    "Tools: latest_signals (recent signals), portfolio (positions/valuation), "
    "market_context (regime, highlighted watchlist, discovery candidates), "
    "performance (evaluation stats), analyze_ticker (tool_arg=TICKER; runs "
    "the full pipeline for any ticker)."
)


class ChatTurn(BaseModel):
    """One model step: either a final reply or a single tool request."""

    reply: str | None = Field(default=None, max_length=4000)
    tool: ToolName | None = None
    tool_arg: str | None = Field(default=None, max_length=12)


# ------------------------------------------------------------------ tools ----
def _signal_summary(row: SignalRow) -> dict:
    return {
        "id": row.id,
        "created_at": str(row.created_at),
        "ticker": row.ticker,
        "action": row.action,
        "shares": row.shares,
        "max_entry_price": float(row.max_entry_price) if row.max_entry_price else None,
        "stop_loss": float(row.stop_loss) if row.stop_loss else None,
        "take_profit": float(row.take_profit) if row.take_profit else None,
        "confidence": row.confidence,
        "risk_score": row.risk_score,
        "strategy": row.strategy,
        "regime": row.regime,
        "explanation": row.explanation,
        "user_decision": row.user_decision,
    }


def _tool_latest_signals(db: Session, _arg: str | None) -> dict:
    from sentinel.pipeline.persist import list_signals

    rows = list_signals(db, limit=10)
    return {"signals": [_signal_summary(r) for r in rows]}


def _tool_portfolio(db: Session, _arg: str | None) -> dict:
    from sentinel.portfolio.state import build_portfolio_state, cash_balance

    state = build_portfolio_state(db)
    return {
        "equity": state.equity,
        "cash": cash_balance(db),
        "open_positions": [
            {
                "symbol": p.symbol,
                "shares": p.shares,
                "price": p.price,
                "value": p.shares * p.price,
                "sector": p.sector,
            }
            for p in state.positions
        ],
        "day_pnl": state.day_pnl,
        "high_water_mark": state.high_water_mark,
    }


def _tool_market_context(db: Session, _arg: str | None) -> dict:
    from sentinel.data.discovery import get_candidates
    from sentinel.pipeline.runner import last_run

    watchlist = get_watchlist(db)
    run = last_run()
    regime = None
    if run is not None and run.regime is not None:
        regime = {
            "regime": run.regime.regime,
            "as_of": str(run.started_at),
            "detail": run.regime.detail,
        }
    return {
        "highlighted_watchlist": watchlist,
        "discovery_candidates": get_candidates(db),
        "last_regime": regime,
    }


def _tool_performance(db: Session, _arg: str | None) -> dict:
    from sentinel.api.routers.analytics import analytics_summary

    return analytics_summary(db)


def _tool_analyze_ticker(db: Session, arg: str | None) -> dict:
    from sentinel.data.ingest import ensure_symbol_data
    from sentinel.pipeline.persist import get_risk_check
    from sentinel.pipeline.runner import run_scan

    if not arg or not arg.strip():
        return {"error": "analyze_ticker requires a ticker symbol in tool_arg"}
    ticker = arg.strip().upper()
    # any ticker is analyzable — backfill data on demand if it was never
    # ingested (e.g. outside the static universe); chat runs never alert
    ensure_symbol_data(db, ticker)
    state = run_scan(db, symbols=[ticker])
    out = []
    for signal in state.signals:
        row = db.get(SignalRow, str(signal.id))
        if row is None:  # persisted in run_scan; defensive
            continue
        check = get_risk_check(db, str(signal.id))
        out.append(
            {
                **_signal_summary(row),
                "risk_check": {
                    "approved": check.approved,
                    "rules": check.rules,
                }
                if check
                else None,
            }
        )
    return {"ticker": ticker, "signals": out}


_TOOLS = {
    "latest_signals": _tool_latest_signals,
    "portfolio": _tool_portfolio,
    "market_context": _tool_market_context,
    "performance": _tool_performance,
    "analyze_ticker": _tool_analyze_ticker,
}


# ------------------------------------------------------------------ engine ----
def _fallback_reply(db: Session) -> str:
    data = _tool_latest_signals(db, None)["signals"]
    lines = [
        "The assistant model is unavailable right now (budget, keys, or outage), "
        "so here is a deterministic summary instead."
    ]
    if data:
        lines.append("Latest signals:")
        lines += [
            f"- {s['created_at'][:16]} {s['ticker']} {s['action']} "
            f"(confidence {s['confidence']:.0%}, {s['strategy']})"
            for s in data[:5]
        ]
    else:
        lines.append("No signals recorded yet.")
    return "\n".join(lines)


def chat_reply(db: Session, message: str, history_limit: int = 12) -> dict:
    """One user turn: persist it, run the tool loop, persist + return the reply."""
    db.add(ChatMessageRow(role="user", content=message))
    db.flush()

    prior = db.execute(
        select(ChatMessageRow).order_by(ChatMessageRow.id.desc()).limit(history_limit)
    ).scalars().all()
    transcript: list[dict] = [
        {"role": r.role, "content": r.content[:2000], "tool": r.tool_name}
        for r in reversed(prior)
    ]

    tool_calls: list[str] = []
    reply: str | None = None
    try:
        for _ in range(MAX_TOOL_CALLS + 1):
            turn = complete_json(
                db,
                role="reasoning",
                system=_SYSTEM,
                user=json.dumps({"transcript": transcript}, default=str),
                schema=ChatTurn,
                endpoint="chat",
            )
            if turn.tool is None or len(tool_calls) >= MAX_TOOL_CALLS:
                reply = turn.reply or "(no reply)"
                break
            tool_calls.append(turn.tool)
            try:
                result = _TOOLS[turn.tool](db, turn.tool_arg)
            except Exception as exc:
                log.warning("chat tool failed", tool=turn.tool, error=str(exc))
                result = {"error": f"{turn.tool} failed: {exc.__class__.__name__}"}
            result_text = json.dumps(result, default=str)[:8000]
            db.add(ChatMessageRow(role="tool", content=result_text, tool_name=turn.tool))
            db.flush()
            transcript.append({"role": "tool", "content": result_text, "tool": turn.tool})
        if reply is None:
            reply = "I hit my tool-call limit for this turn — ask me to continue."
    except LLMError as exc:
        log.warning("chat LLM unavailable", error=str(exc))
        reply = _fallback_reply(db)

    db.add(ChatMessageRow(role="assistant", content=reply))
    db.flush()
    return {"reply": reply, "tool_calls": tool_calls}
