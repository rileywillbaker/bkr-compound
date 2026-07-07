"""Alert history + test send. Alert thresholds live in settings; the router
never sends anything the risk engine did not approve."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from sentinel.alerts.briefs import recent_alerts
from sentinel.alerts.format import FOOTER
from sentinel.alerts.router import alerts_sent_today
from sentinel.alerts.telegram import send_telegram
from sentinel.config import get_settings
from sentinel.db.base import get_db
from sentinel.db.models import AlertRow

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
def history(days: int = 7, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    rows = recent_alerts(db, days=days)
    return {
        "sent_today": alerts_sent_today(db),
        "max_per_day": settings.max_alerts_per_day,
        "confidence_threshold": settings.alert_confidence_threshold,
        "alerts": [
            {
                "id": r.id,
                "ts": r.ts,
                "kind": r.kind,
                "channel": r.channel,
                "signal_id": r.signal_id,
                "ok": r.ok,
                "text": r.text,
                "detail": r.detail,
            }
            for r in rows
        ],
    }


@router.post("/test")
def test_send(db: Session = Depends(get_db)) -> dict:
    """Send a test message to the configured Telegram chat."""
    ok, detail = send_telegram(db, f"B-Quant test alert. {FOOTER}")
    db.add(AlertRow(kind="test", ok=ok, detail=detail, text="test alert"))
    db.commit()
    return {"ok": ok, "detail": detail}
