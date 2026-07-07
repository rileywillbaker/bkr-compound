"""Provider interfaces (spec §2): every external dependency sits behind one of
these ABCs. Swapping providers must require only config changes."""

from abc import ABC, abstractmethod
from datetime import date, datetime

from sentinel.providers.types import (
    Bar,
    BasicFinancials,
    CompanyProfile,
    EarningsEvent,
    Filing,
    InsiderTransaction,
    MacroPoint,
    NewsItem,
    ProviderCheck,
    Quote,
    RecommendationTrend,
)


class ProviderError(Exception):
    """Raised for provider failures the caller can act on."""


class ProviderUnavailable(ProviderError):
    """Endpoint or plan tier not available (e.g. Finnhub premium-gated)."""


class MarketDataProvider(ABC):
    name: str = "market_data"

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]: ...

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...


class ResearchDataProvider(ABC):
    """News/fundamentals/calendars. Each capability is independently optional:
    implementations raise ProviderUnavailable for tier-gated endpoints and the
    caller records that factor as 'unavailable' instead of failing."""

    name: str = "research"

    @abstractmethod
    def company_news(self, symbol: str, start: date, end: date) -> list[NewsItem]: ...

    @abstractmethod
    def market_news(self) -> list[NewsItem]: ...

    @abstractmethod
    def company_profile(self, symbol: str) -> CompanyProfile: ...

    @abstractmethod
    def basic_financials(self, symbol: str) -> BasicFinancials: ...

    @abstractmethod
    def earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]: ...

    @abstractmethod
    def recommendation_trends(self, symbol: str) -> list[RecommendationTrend]: ...

    @abstractmethod
    def insider_transactions(self, symbol: str) -> list[InsiderTransaction]: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...


class MacroDataProvider(ABC):
    name: str = "macro"

    @abstractmethod
    def get_series(self, series_id: str, start: date) -> list[MacroPoint]: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...


class FilingsProvider(ABC):
    name: str = "filings"

    @abstractmethod
    def recent_filings(self, symbol: str, forms: list[str]) -> list[Filing]: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...


class AlertChannel(ABC):
    name: str = "alerts"

    @abstractmethod
    def send(self, text: str) -> bool: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...
