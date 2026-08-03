"""Free financial news collection.

Two complementary passes:

**Broad feeds** — publisher front pages (Yahoo Finance, CNBC sections,
MarketWatch, Seeking Alpha, Investing.com, Nasdaq). These answer "what is the
market talking about at all", which is what makes a *baseline* possible: a
theme's news momentum is only meaningful relative to the overall news volume
of the day.

**Targeted queries** — one Google News RSS search per theme. This is what
finds a story about small modular reactors that no front page carried. Google
News RSS is free, keyless, and returns syndicated results from publishers
whose own feeds are paywalled or discontinued — which is how Reuters coverage
is reached at all, since Reuters retired its public RSS feeds and its site
disallows scraping. We query the aggregator instead of circumventing anyone's
access controls.

Per-publisher notes on what is and isn't reliably free:

  Yahoo Finance   public RSS, stable.
  CNBC            public per-section RSS, stable.
  MarketWatch     Dow Jones public feeds; the legacy marketwatch.com/rss
                  paths were retired, so the current hosts are used.
  Seeking Alpha   public market-currents feed; frequently behind a bot check,
                  so it is expected to fail some days and is not relied upon.
  Reuters         no free feed since 2020 — reached via Google News only.
  Google News     free RSS search, no key, the workhorse of this module.

Nothing here logs in, pays, or evades a paywall. When a publisher declines,
the collector records the miss and moves on.
"""

from __future__ import annotations

from urllib.parse import quote_plus

import structlog

from sentinel.trends.sources.feeds import FeedItem, fetch_feed
from sentinel.trends.taxonomy import THEMES, Theme

log = structlog.get_logger()

# --- broad publisher feeds ------------------------------------------------
# (source key, url). Source keys are stable identifiers stored on every
# document, so coverage reporting can say exactly who answered.
BROAD_FEEDS: tuple[tuple[str, str], ...] = (
    ("yahoo_finance", "https://finance.yahoo.com/news/rssindex"),
    ("cnbc_top", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("cnbc_business", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("cnbc_investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("cnbc_technology", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("cnbc_energy", "https://www.cnbc.com/id/19836768/device/rss/rss.html"),
    ("cnbc_economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("marketwatch_top", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("marketwatch_pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("marketwatch_realtime", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("seekingalpha", "https://seekingalpha.com/market_currents.xml"),
    ("investing_com", "https://www.investing.com/rss/news_25.rss"),
    ("nasdaq", "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
)

_GOOGLE_NEWS = "https://news.google.com/rss/search"
_GOOGLE_SUFFIX = "&hl=en-US&gl=US&ceid=US:en"

# How many keywords per theme become a Google News query. The full keyword
# list would be dozens of HTTP calls per theme for heavily overlapping
# results; the leading terms are the distinctive ones by construction.
QUERY_KEYWORDS_PER_THEME = 3


def google_news_url(query: str, days: int = 2) -> str:
    """Google News RSS search URL, restricted to the last `days` days."""
    return f"{_GOOGLE_NEWS}?q={quote_plus(f'{query} when:{days}d')}{_GOOGLE_SUFFIX}"


def collect_broad(limit_per_feed: int = 60) -> tuple[list[FeedItem], list[str]]:
    """Publisher front-page feeds.

    Returns (items, sources_that_answered) so the caller can distinguish
    "quiet news day" from "half the publishers 403'd us".
    """
    items: list[FeedItem] = []
    answered: list[str] = []
    for source, url in BROAD_FEEDS:
        found = fetch_feed(url, source=source, channel="news")
        if found:
            answered.append(source)
            items.extend(found[:limit_per_feed])
    log.info("broad news collected", items=len(items), sources=len(answered))
    return items, answered


def theme_queries(theme: Theme, count: int = QUERY_KEYWORDS_PER_THEME) -> list[str]:
    """The Google News searches that represent this theme."""
    return list(theme.keywords[:count])


def collect_theme_news(
    themes: tuple[Theme, ...] = THEMES,
    days: int = 2,
    limit_per_query: int = 25,
) -> tuple[list[FeedItem], list[str]]:
    """One Google News RSS search per leading keyword per theme.

    This is where a genuinely emerging story is found: front pages cover what
    is already big, whereas a targeted query surfaces the trade-press item
    from three days before it becomes big.
    """
    items: list[FeedItem] = []
    answered: list[str] = []
    for theme in themes:
        for keyword in theme_queries(theme):
            found = fetch_feed(
                google_news_url(keyword, days=days),
                source=f"google_news:{theme.key}",
                channel="news",
            )
            if found:
                answered.append(f"google_news:{theme.key}:{keyword}")
                items.extend(found[:limit_per_query])
    log.info("theme news collected", items=len(items), queries=len(answered))
    return items, answered


def collect_symbol_news(symbols: list[str], limit: int = 15) -> list[FeedItem]:
    """Yahoo Finance's per-ticker RSS feed, for a short explicit symbol list.

    Used only for names a theme has already surfaced — running it over the
    universe would be hundreds of requests for data the main pipeline's news
    ingest already holds.
    """
    items: list[FeedItem] = []
    for symbol in symbols:
        found = fetch_feed(
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={quote_plus(symbol)}&region=US&lang=en-US",
            source="yahoo_symbol",
            channel="news",
        )
        items.extend(found[:limit])
    return items


def collect(days: int = 2) -> tuple[list[FeedItem], list[str]]:
    """Everything in this module: broad feeds + per-theme searches."""
    broad_items, broad_sources = collect_broad()
    theme_items, theme_sources = collect_theme_news(days=days)
    return broad_items + theme_items, broad_sources + theme_sources
