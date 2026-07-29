"""Fintel institutional-positioning provider — short interest / dark-pool data.

Auth: `X-API-KEY` header (generate a key at fintel.io/u/dev once logged in;
requires a paid plan — the free tier does not include API access). Base URL
per developers.fintel.io: `https://api.fintel.io/v1`.

IMPORTANT — endpoint paths below are best-effort. Fintel's public docs
(developers.fintel.io) describe *categories* of data (short volume,
institutional holdings, ownership) but do not publish a full, logged-out
endpoint reference, and Fintel's own community forum has reports of the
public API being considerably narrower than the web product (e.g. no
confirmed programmatic access to dark-pool prints specifically — that may
be a web-UI-only feature even on paid plans). Treat this provider like the
options-flow analyst stub elsewhere in this codebase: wired up per spec, but
`validate()` against a real key is the actual verification step, and a 404
here should be read as "this endpoint doesn't exist on this plan" rather than
a bug. Every failure degrades to ProviderUnavailable so a missing endpoint
never breaks a scan — the caller just marks the factor unavailable.
"""

from datetime import date

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import (
    InstitutionalDataProvider,
    ProviderError,
    ProviderUnavailable,
)
from sentinel.providers.types import ProviderCheck, ShortInterestSnapshot

BASE = "https://api.fintel.io/v1"


class FintelInstitutional(InstitutionalDataProvider):
    name = "fintel"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            base_url=BASE, headers={"X-API-KEY": api_key}, timeout=15.0
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            resp = self._client.get(path, params=params or {})
        except httpx.HTTPError as exc:
            raise ProviderError(f"fintel request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("fintel credentials rejected")
        if resp.status_code == 404:
            raise ProviderUnavailable(f"fintel endpoint {path} not on this plan")
        if resp.status_code == 429:
            raise ProviderError("fintel rate limit (upstream)")
        if resp.status_code != 200:
            raise ProviderError(f"fintel HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"fintel: non-JSON response: {exc}") from exc

    def short_interest(self, symbol: str) -> ShortInterestSnapshot:
        raw = self._get(f"/ss/{symbol.lower()}/short-interest")
        return ShortInterestSnapshot(
            symbol=symbol,
            as_of=date.fromisoformat(raw["date"]) if raw.get("date") else date.today(),
            short_percent_float=raw.get("shortPctFloat"),
            short_percent_shares_outstanding=raw.get("shortPctSharesOut"),
            days_to_cover=raw.get("daysToCover"),
            short_interest_change_pct=raw.get("shortInterestChangePct"),
            dark_pool_short_volume_pct=raw.get("darkPoolShortVolumePct"),
        )

    def validate(self) -> ProviderCheck:
        try:
            self.short_interest("AAPL")
            return ProviderCheck(provider=self.name, ok=True, detail="API key valid")
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
        except ProviderUnavailable as exc:
            return ProviderCheck(
                provider=self.name,
                ok=False,
                detail=f"key accepted but endpoint unavailable on this plan: {exc}",
            )
