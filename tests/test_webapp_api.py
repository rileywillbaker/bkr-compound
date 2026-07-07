"""Phase 5 backend: settings, auth, chat, analytics, event bus, quiet hours."""

import json
import queue

import pytest
from fastapi.testclient import TestClient

from sentinel.api.main import create_app
from sentinel.data import bus
from sentinel.db.base import get_db
from sentinel.db.models import ChatMessageRow, SignalRow


@pytest.fixture()
def client(db):
    app = create_app()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------------- settings --
def test_settings_defaults_from_env(client):
    body = client.get("/api/settings").json()
    assert body["watchlist"]  # env fallback
    assert body["starting_equity"] > 0
    assert body["onboarding_complete"] is False


def test_watchlist_roundtrip_and_validation(client):
    resp = client.put("/api/settings/watchlist", json={"symbols": ["nvda", "brk.b "]})
    assert resp.status_code == 200
    assert resp.json()["watchlist"] == ["BRK.B", "NVDA"]
    assert client.get("/api/settings").json()["watchlist"] == ["BRK.B", "NVDA"]

    bad = client.put("/api/settings/watchlist", json={"symbols": ["NV DA"]})
    assert bad.status_code == 422


def test_equity_and_onboarding(client):
    assert client.put("/api/settings/equity", json={"starting_equity": 25_000}).status_code == 200
    assert client.get("/api/settings").json()["starting_equity"] == 25_000
    assert client.put("/api/settings/equity", json={"starting_equity": -5}).status_code == 422

    client.post("/api/settings/onboarding-complete")
    assert client.get("/api/settings").json()["onboarding_complete"] is True


def test_quiet_hours_validation(client):
    ok = client.put("/api/settings/quiet-hours", json={"start": "22:00", "end": "07:00"})
    assert ok.status_code == 200
    assert client.put("/api/settings/quiet-hours", json={"start": "22:00", "end": None}).status_code == 422
    assert client.put("/api/settings/quiet-hours", json={"start": "25:00", "end": "07:00"}).status_code == 422
    cleared = client.put("/api/settings/quiet-hours", json={"start": None, "end": None})
    assert cleared.json()["alert_quiet_hours"] is None


def test_quiet_hours_suppress_alerts(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from sentinel.alerts.router import in_quiet_hours
    from sentinel.db.settings_store import QUIET_HOURS_KEY, set_setting

    et = ZoneInfo("America/New_York")
    set_setting(db, QUIET_HOURS_KEY, {"start": "22:00", "end": "07:00"})
    assert in_quiet_hours(db, datetime(2026, 7, 6, 23, 30, tzinfo=et)) is True
    assert in_quiet_hours(db, datetime(2026, 7, 6, 6, 30, tzinfo=et)) is True
    assert in_quiet_hours(db, datetime(2026, 7, 6, 12, 0, tzinfo=et)) is False
    set_setting(db, QUIET_HOURS_KEY, None)
    assert in_quiet_hours(db, datetime(2026, 7, 6, 23, 30, tzinfo=et)) is False


# --------------------------------------------------------------------- auth --
def test_auth_open_in_dev(client):
    body = client.get("/api/auth/me").json()
    assert body["authenticated"] is True
    assert body["auth_required"] is False


def test_auth_enforced_in_prod(db, monkeypatch):
    from sentinel.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    app = create_app()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        assert c.get("/api/settings").status_code == 401
        assert c.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        ok = c.post("/api/auth/login", json={"password": settings.app_password})
        assert ok.status_code == 200
        assert c.get("/api/settings").status_code == 200  # cookie persisted
        c.post("/api/auth/logout")
        assert c.get("/api/settings").status_code == 401


def test_session_token_tamper_rejected():
    from sentinel.api.auth import make_session_token, session_valid

    token = make_session_token()
    assert session_valid(token) is True
    issued, sig = token.rsplit(".", 1)
    assert session_valid(f"{int(issued) + 60}.{sig}") is False
    assert session_valid("garbage") is False
    assert session_valid(None) is False


# --------------------------------------------------------------------- chat --
def test_chat_tool_loop_with_mock_llm(client, db, monkeypatch):
    db.add(
        SignalRow(
            id="sig-1",
            created_at=__import__("datetime").datetime(2026, 7, 6, 15, 0),
            run_id="run-1",
            ticker="NVDA",
            action="NO_TRADE",
            confidence=0.4,
            risk_score=3,
            time_horizon="swing_days",
            strategy="cash",
            regime="range",
            explanation="nothing actionable",
        )
    )
    db.flush()

    from sentinel.agents import chat as chat_mod

    calls = []

    def fake_complete_json(db_, role, system, user, schema, endpoint=""):
        calls.append(json.loads(user))
        if len(calls) == 1:
            return chat_mod.ChatTurn(tool="latest_signals")
        transcript = calls[-1]["transcript"]
        assert any(m["role"] == "tool" and "NVDA" in m["content"] for m in transcript)
        return chat_mod.ChatTurn(reply="Latest signal: NVDA NO_TRADE (confidence 40%).")

    monkeypatch.setattr(chat_mod, "complete_json", fake_complete_json)

    resp = client.post("/api/chat", json={"message": "any signals today?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_calls"] == ["latest_signals"]
    assert "NVDA" in body["reply"]
    assert "disclaimer" in body

    history = client.get("/api/chat/history").json()["messages"]
    roles = [m["role"] for m in history]
    assert roles == ["user", "tool", "assistant"]


def test_chat_falls_back_when_llm_down(client, db, monkeypatch):
    from sentinel.agents import chat as chat_mod
    from sentinel.providers.llm.client import LLMError

    def boom(*args, **kwargs):
        raise LLMError("no key")

    monkeypatch.setattr(chat_mod, "complete_json", boom)
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert "unavailable" in resp.json()["reply"]
    # transcript still persisted
    assert db.query(ChatMessageRow).count() == 2


def test_chat_tool_call_cap(client, monkeypatch):
    from sentinel.agents import chat as chat_mod

    def always_tool(*args, **kwargs):
        return chat_mod.ChatTurn(tool="portfolio")

    monkeypatch.setattr(chat_mod, "complete_json", always_tool)
    resp = client.post("/api/chat", json={"message": "loop forever"})
    assert resp.status_code == 200
    assert len(resp.json()["tool_calls"]) == chat_mod.MAX_TOOL_CALLS


# ---------------------------------------------------------------- analytics --
def test_analytics_summary_counts(client, db):
    from datetime import datetime

    for i, action in enumerate(["BUY", "NO_TRADE", "NO_TRADE"]):
        db.add(
            SignalRow(
                id=f"a-{i}",
                created_at=datetime(2026, 7, 6, 15, i),
                run_id="run-a",
                ticker="SPY",
                action=action,
                confidence=0.5,
                risk_score=3,
                time_horizon="swing_days",
                strategy="cash" if action == "NO_TRADE" else "momentum_swing",
                regime="bull_trend",
            )
        )
    db.flush()
    body = client.get("/api/analytics/summary").json()
    assert body["signals_total"] == 3
    assert body["by_action"] == {"BUY": 1, "NO_TRADE": 2}
    assert body["resolved"]["count"] == 0
    assert body["resolved"]["hit_rate"] is None


# ---------------------------------------------------------------------- bus --
def test_bus_publish_reaches_subscribers():
    q = bus.subscribe()
    try:
        bus.publish("signal", {"ticker": "NVDA"})
        event = q.get(timeout=2)
        assert event["kind"] == "signal"
        assert event["payload"]["ticker"] == "NVDA"
    finally:
        bus.unsubscribe(q)
    bus.publish("signal", {"ticker": "AAPL"})  # after unsubscribe: no error
    with pytest.raises(queue.Empty):
        q.get_nowait()


def test_ws_feed_delivers_events(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["kind"] == "hello"
        bus.publish("signal", {"ticker": "MSFT", "action": "BUY"})
        event = ws.receive_json()
        assert event["kind"] == "signal"
        assert event["payload"]["ticker"] == "MSFT"
        assert "origin" not in event
