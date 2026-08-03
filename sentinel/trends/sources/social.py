"""Social sentiment from free public sources.

What "free" actually allows, honestly stated:

  **Reddit** — the public `.json` view of any subreddit listing is served
  without authentication. Rate limits for anonymous clients are strict, so we
  read one page per subreddit and pace hard. This is the primary social
  source.

  **StockTwits** — the v2 API's trending and per-symbol streams are open
  without a key, though the free tier is throttled aggressively and has been
  progressively locked down. Treated as a bonus: when it answers we use it,
  when it 403s the run continues.

  **X / Twitter** — there is no free tier that permits reading public posts
  since the v2 API was closed to free access; the remaining options are paid
  plans or scraping in breach of the terms of service. `collect_x()` therefore
  returns nothing, always, and reports itself as unavailable so the report can
  say so plainly. This is deliberate: silently producing an empty X signal
  would let a component quietly score zero and drag a trend down, and quietly
  scraping would break both the terms of service and this project's "free"
  constraint in a way the user did not ask for.

Sentiment is scored by `sentinel/trends/sentiment.py` — an offline, free,
finance-tuned lexicon, not a paid API.

An important framing note carried through to `scoring.py`: social volume
measures ATTENTION, not quality. A ticker being loud on Reddit is evidence
that a trade is crowded at least as much as it is evidence that it is good.
The hype guard exists precisely because this source is the easiest to fake.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import structlog

from sentinel.trends.sources.feeds import FeedItem, fetch_json

log = structlog.get_logger()

# Investing-focused subreddits, deliberately weighted toward the more
# analytical ones. WallStreetBets is included because ignoring it would blind
# the hype detector to the single best predictor of a hype cycle.
SUBREDDITS: tuple[str, ...] = (
    "stocks",
    "investing",
    "StockMarket",
    "wallstreetbets",
    "SecurityAnalysis",
    "ValueInvesting",
    "energy",
    "uraniumsqueeze",
    "NuclearPower",
    "artificial",
    "defensestocks",
)

_REDDIT_LISTING = "https://www.reddit.com/r/{sub}/{listing}.json"
_STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols.json"
_STOCKTWITS_SYMBOL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"


def collect_reddit(
    subreddits: tuple[str, ...] = SUBREDDITS,
    listing: str = "hot",
    limit: int = 75,
) -> tuple[list[FeedItem], list[str]]:
    """Public subreddit listings as documents.

    `engagement` carries upvotes + comments, which `scoring.py` uses to weight
    a post: one heavily-discussed thread is a stronger attention signal than
    twenty posts nobody replied to.
    """
    items: list[FeedItem] = []
    answered: list[str] = []

    for sub in subreddits:
        payload = fetch_json(
            _REDDIT_LISTING.format(sub=sub, listing=listing),
            provider="reddit",
            params={"limit": limit, "raw_json": 1},
        )
        if not isinstance(payload, dict):
            continue
        children = (payload.get("data") or {}).get("children")
        if not isinstance(children, list):
            continue
        answered.append(f"reddit:{sub}")
        for child in children:
            if not isinstance(child, dict):
                continue
            post = child.get("data")
            if not isinstance(post, dict) or post.get("stickied"):
                continue
            title = str(post.get("title") or "").strip()
            if not title:
                continue
            created = post.get("created_utc")
            try:
                stamp = datetime.fromtimestamp(float(created or 0), tz=UTC)
            except (TypeError, ValueError, OSError):
                stamp = datetime.now(UTC)
            score = int(post.get("score") or 0)
            comments = int(post.get("num_comments") or 0)
            items.append(
                FeedItem(
                    source=f"reddit:{sub}",
                    channel="social",
                    title=title[:600],
                    summary=str(post.get("selftext") or "")[:2000],
                    url=f"https://www.reddit.com{post.get('permalink', '')}"[:1000],
                    author=str(post.get("author") or "")[:128],
                    published_at=stamp,
                    engagement=max(0, score) + max(0, comments) * 2,
                    extra={"subreddit": sub, "score": score, "comments": comments},
                )
            )
    log.info("reddit collected", items=len(items), subreddits=len(answered))
    return items, answered


def collect_stocktwits_trending() -> tuple[list[FeedItem], list[str]]:
    """StockTwits' own trending-symbols list.

    One request for the whole list, which is the only responsible way to use
    a throttled free endpoint. Each trending symbol becomes a document whose
    title contains its cashtag, so the standard extraction path picks it up.
    """
    payload = fetch_json(_STOCKTWITS_TRENDING, provider="stocktwits")
    if not isinstance(payload, dict):
        return [], []
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        return [], []
    now = datetime.now(UTC)
    items = []
    for entry in symbols:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        items.append(
            FeedItem(
                source="stocktwits_trending",
                channel="social",
                title=f"${ticker} is trending on StockTwits: {entry.get('title', '')}",
                url=f"https://stocktwits.com/symbol/{ticker}",
                published_at=now,
                engagement=int(entry.get("watchlist_count") or 0),
                extra={"symbol": ticker},
            )
        )
    log.info("stocktwits trending collected", items=len(items))
    return items, (["stocktwits_trending"] if items else [])


def collect_stocktwits_symbols(
    symbols: list[str], limit: int = 30
) -> tuple[list[FeedItem], list[str]]:
    """Per-symbol StockTwits streams for a SHORT explicit list.

    Only called for symbols a theme already surfaced. The free tier cannot
    support sweeping a universe, and attempting it just gets the endpoint
    closed for everyone.
    """
    items: list[FeedItem] = []
    answered: list[str] = []
    for symbol in symbols:
        payload = fetch_json(
            _STOCKTWITS_SYMBOL.format(symbol=symbol.upper()), provider="stocktwits"
        )
        if not isinstance(payload, dict):
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        answered.append(f"stocktwits:{symbol.upper()}")
        for message in messages[:limit]:
            if not isinstance(message, dict):
                continue
            body = str(message.get("body") or "").strip()
            if not body:
                continue
            stamp = datetime.now(UTC)
            raw = message.get("created_at")
            if raw:
                with contextlib.suppress(ValueError):
                    stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            # StockTwits posters self-label Bullish/Bearish. That is a real
            # human judgement and worth more than our lexicon's read of a
            # 20-word post, so it is passed through for scoring to prefer.
            entities = message.get("entities")
            declared = None
            if isinstance(entities, dict):
                sentiment = entities.get("sentiment")
                if isinstance(sentiment, dict):
                    declared = str(sentiment.get("basic") or "").lower() or None
            user = message.get("user")
            followers = int((user or {}).get("followers") or 0) if isinstance(user, dict) else 0
            items.append(
                FeedItem(
                    source="stocktwits",
                    channel="social",
                    title=body[:600],
                    url=f"https://stocktwits.com/message/{message.get('id', '')}"[:1000],
                    author=str((user or {}).get("username") or "")[:128]
                    if isinstance(user, dict)
                    else "",
                    published_at=stamp,
                    engagement=min(followers, 100_000),
                    extra={"symbol": symbol.upper(), "declared_sentiment": declared},
                )
            )
    return items, answered


def collect_x() -> tuple[list[FeedItem], list[str]]:
    """X / Twitter: intentionally unavailable on a free-only system.

    Returns no items and no sources, always. See the module docstring — X has
    no free read tier, and the alternatives are either paid or a terms-of-
    service violation. Reporting the gap honestly is better than filling it
    with a signal we cannot legitimately obtain.
    """
    log.debug("x/twitter skipped: no free read access exists")
    return [], []


def collect(symbols: list[str] | None = None) -> tuple[list[FeedItem], list[str]]:
    """Everything free in this module."""
    reddit_items, reddit_sources = collect_reddit()
    twits_items, twits_sources = collect_stocktwits_trending()
    items = reddit_items + twits_items
    sources = reddit_sources + twits_sources
    if symbols:
        symbol_items, symbol_sources = collect_stocktwits_symbols(symbols[:12])
        items += symbol_items
        sources += symbol_sources
    x_items, x_sources = collect_x()
    return items + x_items, sources + x_sources
