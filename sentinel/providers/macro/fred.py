"""FRED macro-data provider.

Series used by the pipeline (see sentinel/data/ingest.py):
  VIXCLS   - CBOE VIX daily close (Alpaca free tier has no index data)
  DFF      - Effective federal funds rate
  T10Y2Y   - 10y-2y treasury spread (yield-curve)
  CPIAUCSL - CPI (all urban consumers)
  UNRATE   - Unemployment rate
"""

from datetime import date

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import MacroDataProvider, ProviderError
from sentinel.providers.types import MacroPoint, ProviderCheck

BASE = "https://api.stlouisfed.org/fred"

CORE_SERIES = ["VIXCLS", "DFF", "T10Y2Y", "CPIAUCSL", "UNRATE"]


class FredMacro(MacroDataProvider):
    name = "fred"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self._api_key = api_key
        self._client = client or httpx.Client(base_url=BASE, timeout=15.0)

    def get_series(self, series_id: str, start: date) -> list[MacroPoint]:
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            resp = self._client.get(
                "/series/observations",
                params={
                    "series_id": series_id,
                    "observation_start": start.isoformat(),
                    "api_key": self._api_key,
                    "file_type": "json",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"fred request failed: {exc}") from exc
        if resp.status_code == 400 and "api_key" in resp.text:
            raise ProviderError("fred API key rejected")
        if resp.status_code != 200:
            raise ProviderError(f"fred HTTP {resp.status_code}: {resp.text[:200]}")
        out: list[MacroPoint] = []
        for obs in resp.json().get("observations") or []:
            value = obs.get("value", ".")
            out.append(
                MacroPoint(
                    series_id=series_id,
                    date=date.fromisoformat(obs["date"]),
                    value=None if value in (".", "") else float(value),
                )
            )
        return out

    def validate(self) -> ProviderCheck:
        try:
            points = self.get_series("VIXCLS", date.today().replace(month=1, day=1))
            return ProviderCheck(
                provider=self.name, ok=True, detail=f"VIXCLS returned {len(points)} points"
            )
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
