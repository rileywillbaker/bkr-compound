"""Trend API surface. No network, no LLM."""

import pytest
from fastapi.testclient import TestClient

from sentinel.api.main import create_app
from sentinel.db.base import get_db
from sentinel.modes import set_mode
from sentinel.trends import review as review_mod
from sentinel.trends.taxonomy import THEMES_BY_KEY
from tests.trend_synth import (
    seed_bars,
    seed_equity,
    seed_fundamentals,
    seed_holdings,
    seed_social,
    seed_theme_corpus,
)

NUCLEAR = THEMES_BY_KEY["nuclear"]


@pytest.fixture()
def client(db, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("unexpected LLM call")

    monkeypatch.setattr(review_mod, "complete_json", explode)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seeded(db):
    seed_equity(db, 5_000.0)
    set_mode(db, "free")
    seed_bars(db, "SPY", start=400.0, drift=0.05)
    for symbol in NUCLEAR.seeds[:8]:
        seed_bars(db, symbol, start=60.0, drift=0.35, volume=4_000_000)
        seed_fundamentals(db, symbol, sector="Utilities", market_cap=20_000.0)
    for etf in NUCLEAR.etfs:
        seed_bars(db, etf, start=30.0, drift=0.15)
    seed_theme_corpus(db, recent_news=30, baseline_news=4, gov=8, social=4)
    seed_holdings(db, "URA", {"CCJ": 4.0}, days_ago=30)
    seed_holdings(db, "URA", {"CCJ": 9.0}, days_ago=0)
    seed_social(db, "CCJ", mentions=12)
    return db


def test_themes_endpoint_lists_the_taxonomy(client):
    body = client.get("/api/trends/themes").json()
    keys = {t["key"] for t in body["themes"]}
    assert {"nuclear", "uranium", "ai", "defense", "cybersecurity"} <= keys
    assert body["tracked_etfs"]


def test_trends_empty_before_any_run(client):
    body = client.get("/api/trends").json()
    assert body["trends"] == []
    assert "disclaimer" in body


def test_collect_and_list_trends(client, seeded, monkeypatch):
    """Collection is forced offline: every source returns nothing, and the
    endpoint must still score themes from what is already stored."""
    monkeypatch.setattr("sentinel.trends.sources.feeds.fetch", lambda *a, **k: None)
    monkeypatch.setattr("sentinel.trends.sources.feeds.fetch_json", lambda *a, **k: None)
    monkeypatch.setattr("sentinel.trends.sources.feeds.post_json", lambda *a, **k: None)

    posted = client.post("/api/trends/collect").json()
    assert posted["llm_calls"] == 0
    assert posted["themes_scored"] > 0

    listed = client.get("/api/trends").json()
    assert listed["trends"]
    top = listed["trends"][0]
    assert top["theme"] == "nuclear"
    assert 0 <= top["score"] <= 100
    assert top["explanation"]
    assert "components" in top


def test_theme_detail_and_history(client, seeded):
    from sentinel.trends import scoring

    scoring.score_all(seeded)
    body = client.get("/api/trends/themes/nuclear").json()
    assert body["theme"]["key"] == "nuclear"
    assert body["current"] is not None
    assert body["current"]["components"]
    assert body["history"]


def test_unknown_theme_is_404(client):
    assert client.get("/api/trends/themes/does-not-exist").status_code == 404


def test_report_endpoints(client, seeded):
    empty = client.get("/api/trends/report").json()
    assert empty["report"] is None

    built = client.post("/api/trends/report").json()
    assert built["llm_calls"] == 0  # free mode
    assert built["mode"] == "free"
    assert "B-Quant Trend Report" in built["text"]
    assert "Not financial advice" in built["text"]

    stored = client.get("/api/trends/report").json()
    assert stored["report"] is not None


def test_report_recommendations_carry_risk_approved_dollars(client, seeded):
    built = client.post("/api/trends/report").json()
    for opportunity in built["report"]["opportunities"]:
        allocation = opportunity["allocation"]
        assert allocation["approved"] is True
        assert allocation["dollars"] > 0
        assert allocation["risk_check"]["approved"] is True


def test_documents_endpoint_exposes_the_evidence(client, seeded):
    body = client.get("/api/trends/documents", params={"theme": "nuclear"}).json()
    assert body["documents"]
    assert all("nuclear" in d["themes"] for d in body["documents"])


def test_documents_filter_by_channel(client, seeded):
    body = client.get("/api/trends/documents", params={"channel": "gov"}).json()
    assert body["documents"]
    assert all(d["channel"] == "gov" for d in body["documents"])


def test_invalid_channel_is_rejected(client):
    assert client.get("/api/trends/documents", params={"channel": "bogus"}).status_code == 422


def test_social_endpoint_reports_attention_with_a_caveat(client, seeded):
    body = client.get("/api/trends/social").json()
    assert body["trending"]
    assert body["trending"][0]["symbol"] == "CCJ"
    assert "attention, not quality" in body["note"]


def test_etf_activity_endpoint(client, seeded):
    body = client.get("/api/trends/etf-activity").json()
    themes = {t["theme"] for t in body["themes"]}
    assert "uranium" in themes or "nuclear" in themes
    assert "no free holdings data" in body["note"]


def test_every_trend_surface_carries_a_disclaimer(client, seeded):
    for path in ("/api/trends", "/api/trends/report", "/api/trends/social"):
        assert "disclaimer" in client.get(path).json()
