"""Finnhub research provider (free tier).

Premium-gated endpoints raise ProviderUnavailable so analysts mark the factor
'unavailable' instead of failing the pipeline.
"""

from datetime import date, datetime

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import (
    ProviderError,
    ProviderUnavailable,
    ResearchDataProvider,
)
from sentinel.providers.types import (
    BasicFinancials,
    CompanyProfile,
    EarningsEvent,
    InsiderTransaction,
    NewsItem,
    ProviderCheck,
    RecommendationTrend,
)

BASE = "https://finnhub.io/api/v1"


class FinnhubResearch(ResearchDataProvider):
    name = "finnhub"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=BASE, params={"token": api_key}, timeout=15.0
        )

    def _get(self, path: str, params: dict | None = None):
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            resp = self._client.get(path, params=params or {})
        except httpx.HTTPError as exc:
            raise ProviderError(f"finnhub request failed: {exc}") from exc
        if resp.status_code == 401:
            raise ProviderError("finnhub credentials rejected")
        if resp.status_code == 403:
            raise ProviderUnavailable(f"finnhub endpoint {path} is premium-gated")
        if resp.status_code == 429:
            raise ProviderError("finnhub rate limit (upstream)")
        if resp.status_code != 200:
            raise ProviderError(f"finnhub HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def company_news(self, symbol: str, start: date, end: date) -> list[NewsItem]:
        data = self._get(
            "/company-news",
            {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
        )
        return [self._news_item(raw, symbol) for raw in data or []]

    def market_news(self) -> list[NewsItem]:
        data = self._get("/news", {"category": "general"})
        return [self._news_item(raw, None) for raw in data or []]

    @staticmethod
    def _news_item(raw: dict, symbol: str | None) -> NewsItem:
        return NewsItem(
            provider_id=str(raw.get("id", "")),
            symbol=symbol,
            headline=raw.get("headline", ""),
            summary=raw.get("summary", ""),
            source=raw.get("source", ""),
            url=raw.get("url", ""),
            published_at=datetime.utcfromtimestamp(raw.get("datetime", 0)),
        )

    def company_profile(self, symbol: str) -> CompanyProfile:
        raw = self._get("/stock/profile2", {"symbol": symbol})
        if not raw:
            raise ProviderError(f"finnhub: no profile for {symbol}")
        return CompanyProfile(
            symbol=symbol,
            name=raw.get("name", ""),
            sector=raw.get("finnhubIndustry", ""),
            market_cap=raw.get("marketCapitalization"),
            exchange=raw.get("exchange", ""),
        )

    def basic_financials(self, symbol: str) -> BasicFinancials:
        raw = self._get("/stock/metric", {"symbol": symbol, "metric": "all"})
        m = raw.get("metric") or {}
        return BasicFinancials(
            symbol=symbol,
            pe=m.get("peTTM"),
            ps=m.get("psTTM"),
            eps_growth_ttm=m.get("epsGrowthTTMYoy"),
            revenue_growth_ttm=m.get("revenueGrowthTTMYoy"),
            beta=m.get("beta"),
            week52_high=m.get("52WeekHigh"),
            week52_low=m.get("52WeekLow"),
        )

    def earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]:
        raw = self._get(
            "/calendar/earnings", {"from": start.isoformat(), "to": end.isoformat()}
        )
        out = []
        for e in raw.get("earningsCalendar") or []:
            if not e.get("symbol") or not e.get("date"):
                continue
            out.append(
                EarningsEvent(
                    symbol=e["symbol"],
                    date=date.fromisoformat(e["date"]),
                    hour=e.get("hour") or "",
                    eps_estimate=e.get("epsEstimate"),
                    eps_actual=e.get("epsActual"),
                    revenue_estimate=e.get("revenueEstimate"),
                    revenue_actual=e.get("revenueActual"),
                )
            )
        return out

    def recommendation_trends(self, symbol: str) -> list[RecommendationTrend]:
        raw = self._get("/stock/recommendation", {"symbol": symbol})
        out = []
        for r in raw or []:
            out.append(
                RecommendationTrend(
                    symbol=symbol,
                    period=date.fromisoformat(r["period"]),
                    strong_buy=r.get("strongBuy", 0),
                    buy=r.get("buy", 0),
                    hold=r.get("hold", 0),
                    sell=r.get("sell", 0),
                    strong_sell=r.get("strongSell", 0),
                )
            )
        return out

    def insider_transactions(self, symbol: str) -> list[InsiderTransaction]:
        raw = self._get("/stock/insider-transactions", {"symbol": symbol})
        out = []
        for t in raw.get("data") or []:
            if not t.get("transactionDate"):
                continue
            out.append(
                InsiderTransaction(
                    symbol=symbol,
                    name=t.get("name", ""),
                    share_change=int(t.get("change", 0) or 0),
                    transaction_date=date.fromisoformat(t["transactionDate"]),
                    transaction_price=t.get("transactionPrice"),
                    filing_date=date.fromisoformat(t["filingDate"])
                    if t.get("filingDate")
                    else None,
                )
            )
        return out

    def validate(self) -> ProviderCheck:
        try:
            self._get("/stock/profile2", {"symbol": "AAPL"})
            return ProviderCheck(provider=self.name, ok=True, detail="API key valid")
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
