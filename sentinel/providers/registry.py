"""Provider factory: builds concrete providers with credentials resolved from
the encrypted DB store (first) or environment (second)."""

from sqlalchemy.orm import Session

from sentinel.providers.credentials import get_credential
from sentinel.providers.filings.edgar import EdgarFilings
from sentinel.providers.institutional.fintel import FintelInstitutional
from sentinel.providers.macro.fred import FredMacro
from sentinel.providers.market_data.alpaca import AlpacaMarketData
from sentinel.providers.research.finnhub import FinnhubResearch
from sentinel.providers.research.yahoo import YahooOverview
from sentinel.providers.screener.finviz import FinvizScreener


class CredentialsMissing(Exception):
    def __init__(self, provider: str, fields: list[str]):
        self.provider = provider
        self.fields = fields
        super().__init__(f"{provider} credentials missing: {', '.join(fields)}")


def build_market_data(db: Session | None = None) -> AlpacaMarketData:
    key = get_credential(db, "alpaca", "api_key")
    secret = get_credential(db, "alpaca", "api_secret")
    missing = [f for f, v in [("api_key", key), ("api_secret", secret)] if not v]
    if missing:
        raise CredentialsMissing("alpaca", missing)
    return AlpacaMarketData(key, secret)


def build_research(db: Session | None = None) -> FinnhubResearch:
    key = get_credential(db, "finnhub", "api_key")
    if not key:
        raise CredentialsMissing("finnhub", ["api_key"])
    return FinnhubResearch(key)


def build_macro(db: Session | None = None) -> FredMacro:
    key = get_credential(db, "fred", "api_key")
    if not key:
        raise CredentialsMissing("fred", ["api_key"])
    return FredMacro(key)


def build_filings(db: Session | None = None) -> EdgarFilings:
    ua = get_credential(db, "edgar", "user_agent")
    if not ua or "@" not in ua:
        raise CredentialsMissing("edgar", ["user_agent (must include contact email)"])
    return EdgarFilings(ua)


def build_overview(db: Session | None = None) -> YahooOverview:
    """Yahoo needs no credentials — always available."""
    return YahooOverview()


def build_screener(db: Session | None = None) -> FinvizScreener:
    token = get_credential(db, "finviz", "auth_token")
    if not token:
        raise CredentialsMissing("finviz", ["auth_token"])
    return FinvizScreener(token)


def build_institutional(db: Session | None = None) -> FintelInstitutional:
    key = get_credential(db, "fintel", "api_key")
    if not key:
        raise CredentialsMissing("fintel", ["api_key"])
    return FintelInstitutional(key)
