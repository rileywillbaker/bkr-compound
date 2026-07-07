"""Signal persistence round-trip, decision capture -> journal, API routers."""

import pytest
from fastapi.testclient import TestClient

from sentinel.api.main import create_app
from sentinel.db.base import get_db
from sentinel.db.models import JournalEntryRow, SignalRow
from sentinel.pipeline.persist import (
    get_risk_check,
    list_signals,
    record_decision,
    save_signals,
)
from sentinel.pipeline.state import PipelineState
from tests.test_alerts import make_signal


@pytest.fixture()
def client(db):
    app = create_app()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


@pytest.fixture()
def saved_signal(db):
    signal = make_signal()
    save_signals(db, PipelineState(symbols=["NVDA"], signals=[signal]))
    return signal


def test_save_signals_round_trip(db, saved_signal):
    row = db.get(SignalRow, str(saved_signal.id))
    assert row is not None
    assert row.ticker == "NVDA"
    assert row.action == "BUY"
    assert row.shares == 18
    assert float(row.max_entry_price) == 875.20
    assert row.confidence == 0.93
    assert row.strategy == "breakout"
    assert row.user_decision is None and not row.alert_sent
    check = get_risk_check(db, str(saved_signal.id))
    assert check is not None and check.approved and check.profile_version == 1


def test_list_signals_filters(db, saved_signal):
    assert len(list_signals(db)) == 1
    assert len(list_signals(db, ticker="nvda")) == 1
    assert len(list_signals(db, ticker="AAPL")) == 0
    assert len(list_signals(db, action="BUY")) == 1
    assert len(list_signals(db, decision="taken")) == 0


def test_record_decision_creates_journal_entry(db, saved_signal):
    row = record_decision(db, str(saved_signal.id), "taken", note="filled at open")
    assert row.user_decision == "taken"
    assert row.decided_at is not None
    [entry] = db.query(JournalEntryRow).all()
    assert entry.signal_id == str(saved_signal.id)
    assert entry.ticker == "NVDA"
    assert entry.decision == "taken"
    assert entry.note == "filled at open"
    assert record_decision(db, "no-such-id", "skipped") is None


def test_signals_api_list_and_detail(client, saved_signal):
    body = client.get("/api/signals").json()
    assert len(body["signals"]) == 1
    assert body["disclaimer"]
    listed = body["signals"][0]
    assert listed["ticker"] == "NVDA"
    assert listed["confidence"] == 0.93

    detail = client.get(f"/api/signals/{saved_signal.id}").json()
    assert detail["risk_check"]["approved"] is True
    assert isinstance(detail["evidence"], list)
    assert detail["disclaimer"]

    assert client.get("/api/signals/nope").status_code == 404


def test_decision_endpoint(client, db, saved_signal):
    response = client.post(
        f"/api/signals/{saved_signal.id}/decision",
        json={"decision": "skipped", "note": "gap up past max price"},
    )
    assert response.status_code == 200
    assert response.json()["user_decision"] == "skipped"

    entries = client.get("/api/signals/journal/entries").json()
    assert len(entries) == 1
    assert entries[0]["decision"] == "skipped"
    assert entries[0]["note"] == "gap up past max price"

    assert client.post(
        "/api/signals/nope/decision", json={"decision": "taken"}
    ).status_code == 404
    assert client.post(
        f"/api/signals/{saved_signal.id}/decision", json={"decision": "yolo"}
    ).status_code == 422


def test_alerts_api(client, db, monkeypatch):
    body = client.get("/api/alerts").json()
    assert body["sent_today"] == 0
    assert body["max_per_day"] == 5
    assert body["confidence_threshold"] == 0.8
    assert body["alerts"] == []

    monkeypatch.setattr(
        "sentinel.api.routers.alerts.send_telegram", lambda db, text: (True, "")
    )
    test_result = client.post("/api/alerts/test").json()
    assert test_result["ok"] is True
    body = client.get("/api/alerts").json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["kind"] == "test"
    assert body["sent_today"] == 0  # test sends do not count against the signal cap


