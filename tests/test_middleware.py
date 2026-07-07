"""Phase 7: rate limiting + security headers."""

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
    with TestClient(app) as c:
        yield c


def test_security_headers_present(client):
    resp = client.get("/api/settings")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Cache-Control"] == "no-store"


def test_health_not_rate_limited_or_no_stored(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] != "no-store"


def test_rate_limit_kicks_in(db):
    from sentinel.api import middleware as mw

    app = create_app()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        # patch after startup: middleware stack is built lazily on first call
        c.get("/api/settings")
        stack = app.middleware_stack
        while stack is not None and not isinstance(stack, mw.RateLimitMiddleware):
            stack = getattr(stack, "app", None)
        assert isinstance(stack, mw.RateLimitMiddleware)
        stack.limit = 3
        stack._counts = {}
        codes = [c.get("/api/settings").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert 429 in codes[3:]
        limited = c.get("/api/settings")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        # /health stays reachable while /api is limited
        assert c.get("/health").status_code == 200
