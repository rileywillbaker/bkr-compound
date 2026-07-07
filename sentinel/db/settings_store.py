"""UI-editable app settings (spec §7.7): watchlist manager, alert quiet
hours, starting equity, onboarding flag.

Values live in the `app_settings` JSON key-value table; the `.env` value is
the fallback so a fresh install works before onboarding completes. Bootstrap
secrets and provider keys are NOT stored here (see config.py and
providers/credentials.py).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from sentinel.config import get_settings
from sentinel.db.models import AppSettingRow

WATCHLIST_KEY = "watchlist"
STARTING_EQUITY_KEY = "starting_equity"
QUIET_HOURS_KEY = "alert_quiet_hours"  # {"start": "22:00", "end": "07:00"} ET
ONBOARDED_KEY = "onboarding_complete"


def get_setting(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(AppSettingRow, key)
    if row is None:
        return default
    return row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(AppSettingRow, key)
    if row is None:
        db.add(AppSettingRow(key=key, value={"v": value}, updated_at=datetime.now(UTC)))
    else:
        row.value = {"v": value}
        row.updated_at = datetime.now(UTC)
    db.flush()


def get_watchlist(db: Session) -> list[str]:
    stored = get_setting(db, WATCHLIST_KEY)
    if isinstance(stored, list) and stored:
        return [str(s).strip().upper() for s in stored if str(s).strip()]
    return get_settings().watchlist_symbols


def set_watchlist(db: Session, symbols: list[str]) -> list[str]:
    cleaned = sorted({s.strip().upper() for s in symbols if s.strip()})
    set_setting(db, WATCHLIST_KEY, cleaned)
    return cleaned


def get_starting_equity(db: Session) -> float:
    stored = get_setting(db, STARTING_EQUITY_KEY)
    if isinstance(stored, (int, float)) and stored > 0:
        return float(stored)
    return get_settings().starting_equity


def is_onboarded(db: Session) -> bool:
    return bool(get_setting(db, ONBOARDED_KEY, False))
