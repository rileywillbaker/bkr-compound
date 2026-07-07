"""Fixed-fractional position sizing (spec §4.5). Deterministic only — no LLM
output ever reaches these numbers.

  risk_amount   = equity * risk_per_trade_pct
  stop_distance = atr_stop_multiple * ATR14
  shares        = floor(risk_amount / stop_distance)
  stop_loss     = entry - stop_distance
  take_profit   = entry + min_reward_risk * stop_distance
"""

import math

from pydantic import BaseModel

from sentinel.risk.profile import RiskProfile


class SizingResult(BaseModel):
    shares: int
    max_entry_price: float
    stop_loss: float
    take_profit: float
    risk_amount: float
    stop_distance: float
    reward_risk: float


def size_position(
    equity: float,
    entry_price: float,
    atr14: float,
    profile: RiskProfile,
    entry_buffer_pct: float = 0.15,
) -> SizingResult | None:
    """Returns None when no valid position exists (zero shares or bad inputs).

    max_entry_price is a limit slightly above the reference price so the user
    doesn't chase; all downstream levels are computed off the reference entry.
    """
    if equity <= 0 or entry_price <= 0 or atr14 <= 0:
        return None
    risk_amount = equity * profile.risk_per_trade_pct / 100
    stop_distance = profile.atr_stop_multiple * atr14
    if stop_distance <= 0:
        return None
    shares = math.floor(risk_amount / stop_distance)
    # Never size above what max_position_pct could ever allow — the risk
    # engine still has final veto, this just avoids absurd drafts.
    max_shares_by_position = math.floor(
        equity * profile.max_position_pct / 100 / entry_price
    )
    shares = min(shares, max_shares_by_position)
    if shares <= 0:
        return None
    stop_loss = entry_price - stop_distance
    if stop_loss <= 0:
        return None
    take_profit = entry_price + profile.min_reward_risk * stop_distance
    return SizingResult(
        shares=shares,
        max_entry_price=round(entry_price * (1 + entry_buffer_pct / 100), 4),
        stop_loss=round(stop_loss, 4),
        take_profit=round(take_profit, 4),
        risk_amount=round(risk_amount, 2),
        stop_distance=round(stop_distance, 6),
        reward_risk=profile.min_reward_risk,
    )
