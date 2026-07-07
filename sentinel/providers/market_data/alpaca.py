"""Alpaca market-data provider (free IEX feed).

Data-plane only: this module talks exclusively to data.alpaca.markets.
No order endpoint exists anywhere in this codebase.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import MarketDataProvider, ProviderError
from sentinel.providers.types import Bar, ProviderCheck, Quote

DATA_BASE = "https://data.alpaca.markets"


class AlpacaMarketData(MarketDataProvider):
    name = "alpaca"

    def __init__(self, api_key: str, api_secret: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=DATA_BASE,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            timeout=15.0,
        )

    def _get(self, path: str, params: dict) -> dict:
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"alpaca request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("alpaca credentials rejected")
        if resp.status_code != 200:
            raise ProviderError(f"alpaca HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]:
        params = {
            "timeframe": timeframe,
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "limit": 10_000,
            "adjustment": "split",
            "feed": "iex",
        }
        if end is not None:
            params["end"] = end.astimezone(UTC).isoformat().replace("+00:00", "Z")

        bars: list[Bar] = []
        page_token: str | None = None
        while True:
            if page_token:
                params["page_token"] = page_token
            data = self._get(f"/v2/stocks/{symbol}/bars", params)
            for raw in data.get("bars") or []:
                bars.append(
                    Bar(
                        symbol=symbol,
                        ts=datetime.fromisoformat(raw["t"].replace("Z", "+00:00")),
                        open=Decimal(str(raw["o"])),
                        high=Decimal(str(raw["h"])),
                        low=Decimal(str(raw["l"])),
                        close=Decimal(str(raw["c"])),
                        volume=int(raw["v"]),
                        timeframe=timeframe,  # type: ignore[arg-type]
                    )
                )
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return bars

    def get_latest_quote(self, symbol: str) -> Quote:
        data = self._get(f"/v2/stocks/{symbol}/quotes/latest", {"feed": "iex"})
        q = data.get("quote") or {}
        return Quote(
            symbol=symbol,
            ts=datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
            if q.get("t")
            else datetime.now(UTC),
            bid=Decimal(str(q["bp"])) if q.get("bp") else None,
            ask=Decimal(str(q["ap"])) if q.get("ap") else None,
        )

    def validate(self) -> ProviderCheck:
        try:
            self._get("/v2/stocks/SPY/bars", {"timeframe": "1Day", "limit": 1, "feed": "iex"})
            return ProviderCheck(provider=self.name, ok=True, detail="IEX feed reachable")
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
