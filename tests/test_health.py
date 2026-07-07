from fastapi.testclient import TestClient

from sentinel.api.main import create_app


def test_health_ok():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_app_description_carries_disclaimer():
    from sentinel import DISCLAIMER

    app = create_app()
    assert DISCLAIMER in (app.description or "")
