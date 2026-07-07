"""SEC EDGAR filings provider (free, no key).

SEC fair-access policy: descriptive User-Agent with contact info, max 10
requests/second (we budget 8/s via the rate limiter).
"""

from datetime import date

import httpx

from sentinel.data.rate_limit import get_rate_limiter
from sentinel.providers.base import FilingsProvider, ProviderError
from sentinel.providers.types import Filing, ProviderCheck

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"


class EdgarFilings(FilingsProvider):
    name = "edgar"

    def __init__(self, user_agent: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=20.0,
        )
        self._cik_cache: dict[str, str] | None = None

    def _get(self, url: str) -> dict:
        get_rate_limiter().wait_and_acquire(self.name)
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(f"edgar request failed: {exc}") from exc
        if resp.status_code == 403:
            raise ProviderError("edgar rejected request — check EDGAR_USER_AGENT format")
        if resp.status_code != 200:
            raise ProviderError(f"edgar HTTP {resp.status_code}")
        return resp.json()

    def _cik_for(self, symbol: str) -> str:
        if self._cik_cache is None:
            data = self._get(TICKERS_URL)
            self._cik_cache = {
                entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
                for entry in data.values()
            }
        cik = self._cik_cache.get(symbol.upper())
        if cik is None:
            raise ProviderError(f"edgar: unknown ticker {symbol}")
        return cik

    def recent_filings(self, symbol: str, forms: list[str]) -> list[Filing]:
        cik = self._cik_for(symbol)
        data = self._get(f"{SUBMISSIONS_BASE}/CIK{cik}.json")
        recent = data.get("filings", {}).get("recent", {})
        wanted = {f.upper() for f in forms}
        out: list[Filing] = []
        for form, filed, accession, doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
            strict=False,
        ):
            if form.upper() not in wanted:
                continue
            acc_nodash = accession.replace("-", "")
            out.append(
                Filing(
                    symbol=symbol.upper(),
                    cik=cik,
                    form=form.upper(),
                    filed_at=date.fromisoformat(filed),
                    accession_no=accession,
                    url=(
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(cik)}/{acc_nodash}/{doc}"
                    ),
                )
            )
        return out

    def validate(self) -> ProviderCheck:
        try:
            self._cik_for("AAPL")
            return ProviderCheck(provider=self.name, ok=True, detail="ticker map loaded")
        except ProviderError as exc:
            return ProviderCheck(provider=self.name, ok=False, detail=str(exc))
