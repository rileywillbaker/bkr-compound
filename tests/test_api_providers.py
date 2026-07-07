"""Provider credential API endpoints (Settings UI backend)."""

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


def test_list_providers_masked(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "alpaca" in body["fields"]
    assert body["fields"]["telegram"] == ["bot_token", "chat_id"]


def test_put_credential_and_masking(client):
    resp = client.put(
        "/api/providers/credentials",
        json={"provider": "finnhub", "field": "api_key", "value": "secret-key-9876"},
    )
    assert resp.status_code == 200
    listing = client.get("/api/providers").json()
    assert listing["configured"]["finnhub"]["api_key"].endswith("9876")
    assert "secret-key" not in listing["configured"]["finnhub"]["api_key"]


def test_put_rejects_empty_and_unknown(client):
    assert (
        client.put(
            "/api/providers/credentials",
            json={"provider": "finnhub", "field": "api_key", "value": "  "},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/providers/credentials",
            json={"provider": "nope", "field": "x", "value": "v"},
        ).status_code
        == 422
    )


def test_test_endpoint_reports_missing_credentials(client, monkeypatch):
    for var in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    from sentinel.config import get_settings

    get_settings.cache_clear()
    try:
        resp = client.post("/api/providers/alpaca/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "missing" in body["detail"]
    finally:
        get_settings.cache_clear()


def test_unknown_provider_404(client):
    assert client.post("/api/providers/bogus/test").status_code == 404
