"""Application settings via pydantic-settings.

Bootstrap secrets (APP_SECRET_KEY, DB/Redis URLs) come from the environment /
`.env`. External provider keys may come from the environment OR from the
encrypted `provider_credentials` table (managed in the Settings UI); the
database value wins. See `sentinel.providers.credentials` for resolution.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- bootstrap ---
    app_secret_key: str = "dev-insecure-secret"
    app_password: str = "change-me"
    database_url: str = "postgresql+psycopg://bquant:bquant@localhost:5432/bquant"
    redis_url: str = "redis://localhost:6379/0"
    app_env: str = "dev"
    log_level: str = "INFO"

    # --- provider keys (optional; DB-stored keys take precedence) ---
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    finnhub_api_key: str = ""
    fred_api_key: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    edgar_user_agent: str = "B-Quant/0.1 (contact@example.com)"

    # --- trading parameters ---
    starting_equity: float = Field(default=10_000, gt=0)
    watchlist: str = "SPY,QQQ,NVDA,AAPL,MSFT"

    # --- alerting (spec §6) ---
    alert_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    max_alerts_per_day: int = Field(default=5, ge=0)

    # --- LLM budget ---
    llm_daily_token_budget: int = 2_000_000

    # --- paths ---
    models_config_path: Path = PROJECT_ROOT / "config" / "models.yaml"

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
