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
    ScreenerRow,
    ShortInterestSnapshot,
    StockOverview,
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


class OverviewProvider(ABC):
    """Keyless supplemental snapshot (Yahoo Finance, unofficial). Enriches
    gaps left by ResearchDataProvider (e.g. Finnhub free-tier gating) —
    never a replacement for it, since this endpoint is unofficial and can
    change or rate-limit without notice."""

    name: str = "overview"

    @abstractmethod
    def overview(self, symbol: str) -> StockOverview: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...


class ScreenerProvider(ABC):
    """Whole-market screener (Finviz Elite export). Unlike ResearchDataProvider
    (one symbol at a time), a screener call returns many matching symbols in a
    single request — including names outside B-Quant's static S&P 500
    universe, which is the point: it is a *discovery* source, not an
    enrichment of symbols already tracked."""

    name: str = "screener"

    @abstractmethod
    def screen(self, filters: list[str]) -> list[ScreenerRow]: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...


class InstitutionalDataProvider(ABC):
    """Short interest / dark-pool / institutional positioning (Fintel).
    Field availability depends on the account's plan tier — callers must
    treat missing fields as 'unavailable', never as zero or bullish/bearish."""

    name: str = "institutional"

    @abstractmethod
    def short_interest(self, symbol: str) -> ShortInterestSnapshot: ...

    @abstractmethod
    def validate(self) -> ProviderCheck: ...
