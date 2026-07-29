"""Yahoo Finance overview provider — unofficial, keyless (spec §2 allows this:
"swapping providers must require only config changes", but there is no config
to set here since Yahoo publishes no supported API or key at all).

Yahoo shut down its public finance API in 2017; every consumer since
(including the popular `yfinance` library) works against the same
undocumented `query1/2.finance.yahoo.com` endpoints. As of this writing those
endpoints require a session cookie + "crumb" token for the quote/quoteSummary
routes, obtained by first hitting a Yahoo page to receive a cookie, then
exchanging it for a crumb. Yahoo can change or block this at any time with no
notice and no support contract — this provider exists purely to fill gaps
left by ResearchDataProvider (e.g. Finnhub's free-tier gating of some
fundamentals fields), never as a primary source. Failures always degrade to
ProviderUnavailable so a bad afternoon on Yahoo's end never breaks a scan.
"""

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import OverviewProvider, ProviderError, ProviderUnavailable
from sentinel.providers.types import ProviderCheck, StockOverview

_CRUMB_BOOTSTRAP_URL = "https://fc.yahoo.com"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class YahooOverview(OverviewProvider):
    name = "yahoo"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            headers={"User-Agent": _UA}, timeout=15.0, follow_redirects=True
        )
        self._crumb: str | None = None

    def _ensure_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            self._client.get(_CRUMB_BOOTSTRAP_URL)  # sets the session cookie
            resp = self._client.get(_CRUMB_URL)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"yahoo: could not establish session: {exc}") from exc
        if resp.status_code != 200 or not resp.text.strip():
            raise ProviderUnavailable(
                f"yahoo: crumb handshake failed (HTTP {resp.status_code}) — "
                "Yahoo may have changed its auth flow"
            )
        self._crumb = resp.text.strip()
        return self._crumb

    def overview(self, symbol: str) -> StockOverview:
        crumb = self._ensure_crumb()
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            resp = self._client.get(
                _QUOTE_URL, params={"symbols": symbol, "crumb": crumb}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"yahoo request failed: {exc}") from exc
        if resp.status_code in (401, 403, 999):
            # crumb likely expired/rejected — force a fresh handshake next call
            self._crumb = None
            raise ProviderUnavailable(f"yahoo: rejected (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise ProviderError(f"yahoo HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            results = resp.json()["quoteResponse"]["result"]
        except (KeyError, ValueError) as exc:
            raise ProviderError(f"yahoo: unexpected response shape: {exc}") from exc
        if not results:
            raise ProviderError(f"yahoo: no quote for {symbol}")
        q = results[0]
        market_cap = q.get("marketCap")
        return StockOverview(
            symbol=symbol,
            price=q.get("regularMarketPrice"),
            pe=q.get("trailingPE"),
            forward_pe=q.get("forwardPE"),
            market_cap=market_cap / 1_000_000 if market_cap else None,
            week52_high=q.get("fiftyTwoWeekHigh"),
            week52_low=q.get("fiftyTwoWeekLow"),
            analyst_target_mean=q.get("targetMeanPrice"),
            analyst_recommendation=q.get("averageAnalystRating"),
            sector=q.get("sector", "") or "",
            short_pct_float=q.get("shortPercentFloat"),
        )

    def validate(self) -> ProviderCheck:
        try:
            self.overview("AAPL")
            return ProviderCheck(provider=self.name, ok=True, detail="reachable")
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
