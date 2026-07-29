"""Finviz Elite screener provider — official CSV export feature, not scraping.

Requires a Finviz *Elite* subscription (finviz.com/elite); the free tier has
no export endpoint. Elite issues an `auth` token (Settings → your profile on
finviz.com once subscribed) that authenticates the export URL below.

Finviz does not formally publish its screener filter-code grammar or column
IDs — the codes below (`FILTER_*`) are the commonly used ones (confirmed via
Finviz's own screener help page and long-standing third-party wrappers) but
are not contractually guaranteed to stay stable. `validate()` and the first
real screen are the actual test: if Finviz changes something, this raises
ProviderError with the raw response for diagnosis rather than silently
returning wrong data. Column parsing is intentionally generic (whatever
headers the export returns) instead of hardcoding column IDs, since those are
even less documented than the filters.
"""

import csv
import io

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import ProviderError, ScreenerProvider
from sentinel.providers.types import ProviderCheck, ScreenerRow

BASE = "https://elite.finviz.com/export"

# 52-week-high/low filter buckets ("N% or more below High"). Verify against
# the live screener (finviz.com/screener.ashx, apply the filter, read the
# resulting f= query param) if a screen ever comes back empty unexpectedly.
FILTER_52W_BELOW_HIGH = {
    5: "ta_highlow52w_b5",
    10: "ta_highlow52w_b10",
    15: "ta_highlow52w_b15",
    20: "ta_highlow52w_b20",
    30: "ta_highlow52w_b30",
    40: "ta_highlow52w_b40",
    50: "ta_highlow52w_b50",
}
FILTER_USA = "geo_usa"


def _price_over_filter(min_price: float) -> str:
    bucket = min(b for b in (1, 2, 3, 5, 7, 10, 15, 20, 30, 40, 50) if b >= min_price) \
        if min_price <= 50 else 50
    return f"sh_price_o{bucket}"


def _avgvol_over_filter(min_avg_volume: int) -> str:
    # Finviz buckets average volume in whole millions >=1, or per-hundred-K below 1M.
    if min_avg_volume >= 1_000_000:
        bucket = max(1, round(min_avg_volume / 1_000_000))
        return f"sh_avgvol_o{bucket}"
    bucket = max(50, round(min_avg_volume / 1000 / 100) * 100)
    return f"sh_avgvol_o{bucket}"


class FinvizScreener(ScreenerProvider):
    name = "finviz"

    def __init__(self, auth_token: str, client: httpx.Client | None = None):
        self._auth = auth_token
        self._client = client or httpx.Client(timeout=20.0)

    def screen(self, filters: list[str]) -> list[ScreenerRow]:
        get_rate_limiter().wait_and_acquire(self.name)
        params = {"v": "111", "ft": "4", "auth": self._auth}
        if filters:
            params["f"] = ",".join(filters)
        try:
            resp = self._client.get(BASE, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"finviz request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("finviz credentials rejected (Elite subscription required)")
        if resp.status_code != 200:
            raise ProviderError(f"finviz HTTP {resp.status_code}: {resp.text[:200]}")
        text = resp.text.strip()
        if not text or text.lstrip().startswith("<"):
            # HTML back instead of CSV usually means auth/plan rejected the request
            raise ProviderError("finviz did not return CSV — check auth token / Elite plan")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw in reader:
            symbol = (raw.get("Ticker") or "").strip().upper()
            if not symbol:
                continue
            price_raw = raw.get("Price")
            price: float | None = None
            if price_raw not in (None, "", "-"):
                try:
                    price = float(str(price_raw))
                except ValueError:
                    price = None
            rows.append(ScreenerRow(symbol=symbol, price=price, raw=dict(raw)))
        return rows

    def pullback_candidates(
        self,
        min_pct_below_high: int = 30,
        min_price: float = 10.0,
        min_avg_volume: int = 500_000,
    ) -> list[ScreenerRow]:
        """Stocks trading well below their 52-week high with enough liquidity
        to actually trade — the "buy the dip" screen. `min_pct_below_high`
        snaps to the nearest Finviz bucket at or below the requested value."""
        bucket = max((b for b in FILTER_52W_BELOW_HIGH if b <= min_pct_below_high), default=5)
        filters = [
            FILTER_USA,
            FILTER_52W_BELOW_HIGH[bucket],
            _price_over_filter(min_price),
            _avgvol_over_filter(min_avg_volume),
        ]
        return self.screen(filters)

    def validate(self) -> ProviderCheck:
        try:
            self.screen([FILTER_USA, "sh_price_o5"])
            return ProviderCheck(provider=self.name, ok=True, detail="export reachable")
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
