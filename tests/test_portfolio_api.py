import pytest
from fastapi.testclient import TestClient

from sentinel.api.main import create_app
from sentinel.db.base import get_db


@pytest.fixture()
def client(db):
    app = create_app()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_buy_creates_position_with_average_cost(client):
    r1 = client.post(
        "/api/portfolio/trades",
        json={"symbol": "nvda", "side": "BUY", "shares": 10, "price": 100.0},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/portfolio/trades",
        json={"symbol": "NVDA", "side": "BUY", "shares": 10, "price": 110.0},
    )
    body = r2.json()
    assert body["shares"] == 20
    assert body["cost_basis"] == 105.0


def test_sell_reduces_and_rejects_oversell(client):
    client.post(
        "/api/portfolio/trades",
        json={"symbol": "AAPL", "side": "BUY", "shares": 5, "price": 200.0},
    )
    ok = client.post(
        "/api/portfolio/trades",
        json={"symbol": "AAPL", "side": "SELL", "shares": 3, "price": 210.0},
    )
    assert ok.json()["shares"] == 2
    bad = client.post(
        "/api/portfolio/trades",
        json={"symbol": "AAPL", "side": "SELL", "shares": 99, "price": 210.0},
    )
    assert bad.status_code == 422


def test_valuation_reports_positions_and_day_pnl(client, monkeypatch):
    monkeypatch.setenv("STARTING_EQUITY", "10000")
    from sentinel.config import get_settings

    get_settings.cache_clear()
    try:
        client.post(
            "/api/portfolio/trades",
            json={"symbol": "MSFT", "side": "BUY", "shares": 10, "price": 400.0},
        )
        body = client.get("/api/portfolio").json()
        # cash = 10000 - 4000; no quote/bar -> mark falls back to cost basis
        assert body["cash"] == 6000.0
        assert body["equity"] == 10_000.0
        assert body["positions"][0]["symbol"] == "MSFT"
        assert body["positions"][0]["market_value"] == 4000.0
    finally:
        get_settings.cache_clear()


def test_trade_validation(client):
    assert (
        client.post(
            "/api/portfolio/trades",
            json={"symbol": "X", "side": "HOLD", "shares": 1, "price": 1},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/portfolio/trades",
            json={"symbol": "X", "side": "BUY", "shares": 0, "price": 1},
        ).status_code
        == 422
    )


def test_risk_profile_versioning(client):
    v1 = client.get("/api/risk/profile").json()
    assert v1["version"] == 1
    assert v1["max_position_pct"] == 10.0
    updated = client.put("/api/risk/profile", json={"max_position_pct": 12.5}).json()
    assert updated["version"] == 2
    assert updated["max_position_pct"] == 12.5
    versions = client.get("/api/risk/profile/versions").json()
    assert [v["version"] for v in versions] == [2, 1]


def test_risk_profile_rejects_invalid(client):
    resp = client.put("/api/risk/profile", json={"max_position_pct": -5})
    assert resp.status_code == 422
