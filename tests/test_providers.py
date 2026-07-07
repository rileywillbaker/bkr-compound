"""Provider parsing tests with mocked HTTP (respx)."""

from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from sentinel.providers.base import ProviderError, ProviderUnavailable
from sentinel.providers.filings.edgar import EdgarFilings
from sentinel.providers.macro.fred import FredMacro
from sentinel.providers.market_data.alpaca import AlpacaMarketData
from sentinel.providers.research.finnhub import FinnhubResearch


@pytest.fixture(autouse=True)
def fresh_rate_window(monkeypatch):
    import sentinel.data.rate_limit as rl

    monkeypatch.setattr(rl, "_local", rl._LocalWindow())
    monkeypatch.setattr(rl, "_limiter", rl.RateLimiter(redis_url="redis://127.0.0.1:1/0"))


# ---------------------------------------------------------------- Alpaca ----
@respx.mock
def test_alpaca_bars_parse_and_paginate():
    respx.get("https://data.alpaca.markets/v2/stocks/NVDA/bars").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "bars": [
                        {
                            "t": "2026-07-01T04:00:00Z",
                            "o": 100.5,
                            "h": 101.0,
                            "l": 99.5,
                            "c": 100.9,
                            "v": 123456,
                        }
                    ],
                    "next_page_token": "abc",
                },
            ),
            httpx.Response(
                200,
                json={
                    "bars": [
                        {
                            "t": "2026-07-02T04:00:00Z",
                            "o": 101.0,
                            "h": 102.0,
                            "l": 100.0,
                            "c": 101.5,
                            "v": 654321,
                        }
                    ],
                    "next_page_token": None,
                },
            ),
        ]
    )
    md = AlpacaMarketData("k", "s")
    bars = md.get_bars("NVDA", "1Day", datetime(2026, 6, 1, tzinfo=UTC))
    assert len(bars) == 2
    assert bars[0].symbol == "NVDA"
    assert float(bars[0].close) == 100.9
    assert bars[1].ts == datetime(2026, 7, 2, 4, 0, tzinfo=UTC)


@respx.mock
def test_alpaca_bad_credentials():
    respx.get("https://data.alpaca.markets/v2/stocks/SPY/bars").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )
    md = AlpacaMarketData("bad", "bad")
    check = md.validate()
    assert not check.ok
    assert "rejected" in check.detail


# --------------------------------------------------------------- Finnhub ----
@respx.mock
def test_finnhub_company_news():
    respx.get("https://finnhub.io/api/v1/company-news").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "headline": "NVDA beats",
                    "summary": "big beat",
                    "source": "Reuters",
                    "url": "http://x",
                    "datetime": 1751500000,
                }
            ],
        )
    )
    fh = FinnhubResearch("k")
    items = fh.company_news("NVDA", date(2026, 7, 1), date(2026, 7, 3))
    assert items[0].headline == "NVDA beats"
    assert items[0].provider_id == "42"
    assert items[0].symbol == "NVDA"


@respx.mock
def test_finnhub_premium_gated_maps_to_unavailable():
    respx.get("https://finnhub.io/api/v1/stock/insider-transactions").mock(
        return_value=httpx.Response(403, text="premium")
    )
    fh = FinnhubResearch("k")
    with pytest.raises(ProviderUnavailable):
        fh.insider_transactions("NVDA")


@respx.mock
def test_finnhub_profile_and_metrics():
    respx.get("https://finnhub.io/api/v1/stock/profile2").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "NVIDIA Corp",
                "finnhubIndustry": "Semiconductors",
                "marketCapitalization": 3000000,
                "exchange": "NASDAQ",
            },
        )
    )
    respx.get("https://finnhub.io/api/v1/stock/metric").mock(
        return_value=httpx.Response(
            200, json={"metric": {"peTTM": 55.5, "beta": 1.7, "52WeekHigh": 150.0}}
        )
    )
    fh = FinnhubResearch("k")
    profile = fh.company_profile("NVDA")
    assert profile.sector == "Semiconductors"
    fin = fh.basic_financials("NVDA")
    assert fin.pe == 55.5
    assert fin.week52_high == 150.0


# ------------------------------------------------------------------ FRED ----
@respx.mock
def test_fred_series_parses_missing_values():
    respx.get("https://api.stlouisfed.org/fred/series/observations").mock(
        return_value=httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2026-07-01", "value": "17.32"},
                    {"date": "2026-07-02", "value": "."},
                ]
            },
        )
    )
    fred = FredMacro("k")
    points = fred.get_series("VIXCLS", date(2026, 7, 1))
    assert points[0].value == 17.32
    assert points[1].value is None


@respx.mock
def test_fred_bad_key():
    respx.get("https://api.stlouisfed.org/fred/series/observations").mock(
        return_value=httpx.Response(400, text="Bad Request. api_key invalid")
    )
    fred = FredMacro("bad")
    with pytest.raises(ProviderError):
        fred.get_series("VIXCLS", date(2026, 1, 1))


# ----------------------------------------------------------------- EDGAR ----
@respx.mock
def test_edgar_recent_filings():
    respx.get("https://www.sec.gov/files/company_tickers.json").mock(
        return_value=httpx.Response(
            200, json={"0": {"ticker": "NVDA", "cik_str": 1045810, "title": "NVIDIA"}}
        )
    )
    respx.get("https://data.sec.gov/submissions/CIK0001045810.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "filings": {
                    "recent": {
                        "form": ["8-K", "4", "10-Q", "SC 13G"],
                        "filingDate": ["2026-07-01", "2026-06-30", "2026-06-01", "2026-05-01"],
                        "accessionNumber": [
                            "0001-26-000001",
                            "0001-26-000002",
                            "0001-26-000003",
                            "0001-26-000004",
                        ],
                        "primaryDocument": ["a.htm", "b.xml", "c.htm", "d.htm"],
                    }
                }
            },
        )
    )
    edgar = EdgarFilings("B-Quant/0.1 (test@example.com)")
    filings = edgar.recent_filings("NVDA", ["8-K", "4"])
    assert {f.form for f in filings} == {"8-K", "4"}
    assert filings[0].cik == "0001045810"
    assert "sec.gov/Archives" in filings[0].url
