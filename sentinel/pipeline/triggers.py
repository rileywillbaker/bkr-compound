"""Event-based AI gating: what makes a candidate worth an LLM call.

Deterministic code answers "is this a good setup?". This module answers the
separate, purely economic question: "has something happened here that code
cannot already explain?" If the answer is no, no call is made — the signal
still ships, deterministic and free.

A candidate must FIRST have cleared every deterministic filter and the risk
engine (that gating lives in pipeline/graph.py). This module then requires at
least one of:

  earnings        — results landed or are imminent
  filing          — a fresh 8-K
  news            — material-event headline or a news-volume spike
  breakout        — technical breakout / new 52-week-high territory
  pullback        — meaningful drawdown from the 52-week high in an uptrend
  volume          — unusual volume vs. its own 20-day average
  conviction      — a high-confidence deterministic setup on its own merits
  position        — the candidate is an open position with a proposed change
  requested       — the user asked directly (always wins)

Triggers are ranked and hard-capped by the operating mode's
max_llm_candidates_per_scan, so a chaotic market day cannot blow the budget:
the cap binds before the trigger list does.
"""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Discovery event kinds that count as a material event, mapped to the trigger
# label surfaced to the user (and to a priority weight).
_EVENT_TRIGGERS: dict[str, tuple[str, float]] = {
    "earnings_surprise": ("earnings", 3.0),
    "high_impact_news": ("news", 2.5),
    "fresh_filing": ("filing", 1.5),
    "unusual_volume": ("volume", 1.5),
    "macro_move": ("volume", 1.5),
    "insider_cluster": ("insider", 2.0),
    "pullback_from_high": ("pullback", 2.0),
    "uptrend_pullback": ("pullback", 2.5),
    "breakout": ("breakout", 2.5),
    "relative_strength": ("momentum", 1.5),
    "sector_leadership": ("momentum", 1.0),
    "earnings_revision": ("earnings", 2.0),
    "elevated_short_interest": ("short-interest", 1.0),
    "finviz_screen": ("screen", 1.0),
}

# A setup this strong justifies a call even on a quiet news day.
HIGH_CONVICTION = 0.62
# Breakout / unusual-volume thresholds computed from the snapshot itself, so
# the trigger works even when discovery hasn't run (e.g. an on-demand scan).
BREAKOUT_PCT_FROM_HIGH = -1.5
UNUSUAL_RVOL = 2.0


class LLMTrigger(BaseModel):
    """Why one candidate earned an LLM call, and how strongly."""

    symbol: str
    reason: str
    priority: float = Field(ge=0)
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


def discovery_events_by_symbol(db: Session, max_age_hours: int = 30) -> dict[str, list[dict]]:
    """Today's persisted discovery events, grouped by symbol.

    Reads the same app_settings blob the scan-symbol selection uses — no new
    queries, no provider calls, no cost.
    """
    from sentinel.data.discovery import DISCOVERY_KEY
    from sentinel.db.settings_store import get_setting

    stored = get_setting(db, DISCOVERY_KEY)
    if not isinstance(stored, dict):
        return {}
    try:
        as_of = datetime.fromisoformat(str(stored.get("as_of")))
    except (TypeError, ValueError):
        return {}
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    if datetime.now(UTC) - as_of > timedelta(hours=max_age_hours):
        return {}
    grouped: dict[str, list[dict]] = {}
    for event in stored.get("events") or []:
        symbol = str(event.get("symbol", "")).upper()
        if symbol:
            grouped.setdefault(symbol, []).append(event)
    return grouped


def evaluate_trigger(
    symbol: str,
    conviction: float,
    events: list[dict] | None = None,
    pct_from_52w_high: float | None = None,
    relative_volume: float | None = None,
    is_open_position: bool = False,
    user_requested: bool = False,
    min_conviction: float = 0.0,
) -> LLMTrigger | None:
    """Best trigger for one candidate, or None when nothing material happened.

    Pure function — all the I/O happened in the caller. `conviction` is the
    deterministic agreement × strategy-fit product (see the synthesizer), not
    the final calibrated confidence.
    """
    if user_requested:
        return LLMTrigger(
            symbol=symbol, reason="requested", priority=10.0, detail="you asked for this one"
        )

    candidates: list[LLMTrigger] = []

    for event in events or []:
        mapped = _EVENT_TRIGGERS.get(str(event.get("kind", "")))
        if mapped is None:
            continue
        reason, weight = mapped
        candidates.append(
            LLMTrigger(
                symbol=symbol,
                reason=reason,
                priority=weight + float(event.get("score", 0.0)),
                detail=str(event.get("detail", ""))[:160],
            )
        )

    if pct_from_52w_high is not None and pct_from_52w_high >= BREAKOUT_PCT_FROM_HIGH:
        candidates.append(
            LLMTrigger(
                symbol=symbol,
                reason="breakout",
                priority=2.5,
                detail=f"{pct_from_52w_high:+.1f}% from the 52-week high",
            )
        )
    if relative_volume is not None and relative_volume >= UNUSUAL_RVOL:
        candidates.append(
            LLMTrigger(
                symbol=symbol,
                reason="volume",
                priority=1.5,
                detail=f"volume {relative_volume:.1f}× its 20-day average",
            )
        )
    if is_open_position:
        candidates.append(
            LLMTrigger(
                symbol=symbol,
                reason="position",
                priority=2.0,
                detail="open position with a proposed change",
            )
        )
    if conviction >= HIGH_CONVICTION:
        candidates.append(
            LLMTrigger(
                symbol=symbol,
                reason="conviction",
                priority=1.0 + conviction,
                detail=f"high-confidence deterministic setup ({conviction:.0%})",
            )
        )

    if not candidates:
        return None
    # A material event justifies the call; a merely-adequate setup with no
    # event does not. The conviction floor applies to the event path too, so
    # noisy news on a weak setup stays free.
    if conviction < min_conviction:
        return None
    return max(candidates, key=lambda t: t.priority)


def rank_and_cap(triggers: list[LLMTrigger], max_calls: int) -> list[LLMTrigger]:
    """Highest-priority triggers first, truncated to the mode's hard cap."""
    if max_calls <= 0:
        return []
    return sorted(triggers, key=lambda t: (-t.priority, t.symbol))[:max_calls]
