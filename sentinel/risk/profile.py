"""RiskProfile: user-editable, versioned limits (spec §5).

Limits change ONLY via explicit user edits in Settings (each edit creates a
new version). Nothing in the system auto-tunes them.
"""

from pydantic import BaseModel, Field


class RiskProfile(BaseModel):
    version: int = 1

    # Position sizing (used by the deterministic sizing agent)
    risk_per_trade_pct: float = Field(default=0.75, gt=0, le=5)
    atr_stop_multiple: float = Field(default=2.0, gt=0, le=10)
    min_reward_risk: float = Field(default=2.0, ge=1)

    # Hard limits (ALL must pass; no override path exists)
    max_position_pct: float = Field(default=10.0, gt=0, le=100)
    max_open_positions: int = Field(default=8, gt=0, le=100)
    max_daily_loss_pct: float = Field(default=2.0, gt=0, le=100)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    max_sector_pct: float = Field(default=25.0, gt=0, le=100)
    max_correlated_exposure_pct: float = Field(default=30.0, gt=0, le=100)
    correlation_threshold: float = Field(default=0.7, ge=0, le=1)
    min_avg_dollar_volume: float = Field(default=5_000_000, ge=0)
    max_adv_participation_pct: float = Field(default=1.0, gt=0, le=100)
    max_atr_pct: float = Field(default=8.0, gt=0, le=100)
    earnings_blackout_days: int = Field(default=2, ge=0)
    max_portfolio_exposure_pct: float = Field(default=100.0, gt=0, le=200)

    # Alerting (spec §6)
    alert_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    max_alerts_per_day: int = Field(default=5, gt=0)
