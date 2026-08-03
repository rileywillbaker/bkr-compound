"""Collection orchestrator: free sources → enriched rows in the database.

One pass per day gathers every source, runs each document through
deterministic theme matching, ticker extraction and lexicon sentiment, and
writes it once. Everything downstream (scoring, ranking, the report, the API)
then reads the database rather than the network, which means:

  * the expensive, flaky part happens exactly once;
  * scoring is reproducible — re-running it gives the same answer;
  * a source being down affects one day's coverage, not the whole history.

Cost: zero. No LLM is involved at any point in this module, and every source
is keyless and free. This is the stage that runs over the whole corpus, and
per CLAUDE.md's cost rules it is exactly the stage that must never call a
model.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.db.models import SocialMentionRow, SystemEvent, TrendDocumentRow
from sentinel.trends import sentiment as sentiment_engine
from sentinel.trends.extract import build_name_index, extract_symbols, extract_themes
from sentinel.trends.sources import etf as etf_source
from sentinel.trends.sources import government, news, social
from sentinel.trends.sources.feeds import FeedItem
from sentinel.trends.taxonomy import seed_symbols, thematic_etfs

log = structlog.get_logger()

# Documents older than this are not stored: a two-week-old headline tells us
# nothing about what is emerging now, and the scoring windows are shorter.
MAX_DOCUMENT_AGE_DAYS = 21


class CollectionResult(BaseModel):
    """What one collection pass actually achieved — the coverage record."""

    as_of: datetime
    documents_seen: int = 0
    documents_stored: int = 0
    by_channel: dict[str, int] = Field(default_factory=dict)
    sources_answered: list[str] = Field(default_factory=list)
    sources_failed: list[str] = Field(default_factory=list)
    etfs_with_holdings: list[str] = Field(default_factory=list)
    holdings_rows: int = 0
    social_symbols: int = 0

    @property
    def news_ok(self) -> bool:
        return any(s.startswith(("yahoo", "cnbc", "marketwatch", "google_news")) for s in self.sources_answered)

    @property
    def gov_ok(self) -> bool:
        return any(
            s.startswith(("federal_register", "usaspending", "dod", "doe", "nrc", "whitehouse"))
            for s in self.sources_answered
        )

    @property
    def social_ok(self) -> bool:
        return any(s.startswith(("reddit", "stocktwits")) for s in self.sources_answered)


def known_symbols(db: Session) -> frozenset[str]:
    """The allow-list ticker extraction is restricted to.

    Universe ∪ taxonomy seeds ∪ tracked ETFs. Nothing outside this set can
    ever reach a recommendation, which is the guard against a garbled feed
    inventing a holding.
    """
    from sentinel.data.universe import get_universe

    return frozenset(
        {s.upper() for s in get_universe(db)}
        | {s.upper() for s in seed_symbols()}
        | {s.upper() for s in thematic_etfs()}
    )


def social_focus_symbols(db: Session, limit: int = 12) -> list[str]:
    """The short symbol list worth spending throttled social calls on.

    Collection runs before scoring, so this reads YESTERDAY's snapshots and
    takes the leading names from the strongest themes — a deliberate one-day
    lag, which is fine because a theme's constituents barely change day to day.

    Falls back to one seed per theme on a cold install so the first run still
    produces theme-attached social content. Round-robins across themes rather
    than draining the top one, so a single dominant theme cannot consume the
    whole budget.
    """
    from sentinel.trends.scoring import latest_snapshots
    from sentinel.trends.taxonomy import THEMES

    ranked: list[list[str]] = []
    try:
        for snapshot in latest_snapshots(db, limit=6):
            symbols = [str(s).upper() for s in (snapshot.symbols or [])]
            if symbols:
                ranked.append(symbols)
    except Exception:
        log.exception("social focus lookup failed")

    if not ranked:
        ranked = [[t.seeds[0]] for t in THEMES if t.seeds]

    out: list[str] = []
    for index in range(limit):
        for symbols in ranked:
            if index < len(symbols) and symbols[index] not in out:
                out.append(symbols[index])
                if len(out) >= limit:
                    return out
    return out[:limit]


def _enrich(
    item: FeedItem, allowed: frozenset[str], name_index: dict[str, str]
) -> tuple[list[str], list[str], float]:
    """Themes, symbols and sentiment for one document. Pure and deterministic."""
    text = item.text
    themes = extract_themes(text)

    # A government source can carry a theme hint from its query even when the
    # document's own wording doesn't contain a taxonomy keyword (a contract
    # award reads "awarded $40,000,000 to ...", which matches nothing).
    hint = item.extra.get("theme_hint")
    if isinstance(hint, str) and hint and hint not in themes:
        themes.append(hint)

    symbols = extract_symbols(text, allowed, name_index)
    explicit = item.extra.get("symbol")
    if isinstance(explicit, str) and explicit.upper() in allowed and explicit.upper() not in symbols:
        symbols.append(explicit.upper())

    # A poster's own Bullish/Bearish label beats our lexicon's read of a
    # 20-word message — it is a human judgement about the same text.
    declared = item.extra.get("declared_sentiment")
    if declared == "bullish":
        score = 0.6
    elif declared == "bearish":
        score = -0.6
    else:
        score = sentiment_engine.score_text(text)
    return themes, sorted(set(symbols)), score


def store_documents(
    db: Session, items: list[FeedItem], allowed: frozenset[str] | None = None
) -> tuple[int, dict[str, int]]:
    """Enrich and upsert documents, skipping duplicates and stale items.

    Returns (stored_count, per-channel counts). Dedup is by `doc_key`, so a
    story appearing in both a front-page feed and a theme query is counted
    once for that source.
    """
    if not items:
        return 0, {}
    allow = allowed if allowed is not None else known_symbols(db)
    name_index = build_name_index(db)
    cutoff = datetime.now(UTC) - timedelta(days=MAX_DOCUMENT_AGE_DAYS)

    stored = 0
    by_channel: dict[str, int] = {}
    seen_this_pass: set[str] = set()

    for item in items:
        published = item.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        if published < cutoff:
            continue
        key = item.doc_key()
        if key in seen_this_pass or db.get(TrendDocumentRow, key) is not None:
            continue
        seen_this_pass.add(key)

        themes, symbols, score = _enrich(item, allow, name_index)
        # A social post about nothing in the taxonomy and no known ticker is
        # noise; storing it would only dilute the baselines.
        if item.channel == "social" and not themes and not symbols:
            continue

        db.add(
            TrendDocumentRow(
                doc_key=key,
                source=item.source[:48],
                channel=item.channel[:16],
                title=item.title,
                summary=item.summary,
                url=item.url,
                author=item.author,
                published_at=published,
                themes=themes,
                symbols=symbols,
                sentiment=score,
                engagement=item.engagement,
            )
        )
        stored += 1
        by_channel[item.channel] = by_channel.get(item.channel, 0) + 1

    db.flush()
    return stored, by_channel


def rebuild_social_aggregates(db: Session, day: date | None = None) -> int:
    """Recompute per-symbol social aggregates for one day from stored documents.

    Derived rather than accumulated, so a partial collection followed by a
    retry produces the correct total instead of double-counting.
    """
    target = day or date.today()
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)

    rows = db.execute(
        select(
            TrendDocumentRow.source,
            TrendDocumentRow.symbols,
            TrendDocumentRow.sentiment,
            TrendDocumentRow.engagement,
        ).where(
            TrendDocumentRow.channel == "social",
            TrendDocumentRow.published_at >= start,
            TrendDocumentRow.published_at < end,
        )
    ).all()

    # (symbol, source_family) -> accumulator. Source family collapses
    # "reddit:stocks" and "reddit:investing" into "reddit" so the stored
    # aggregate is per-platform, not per-subreddit.
    buckets: dict[tuple[str, str], dict] = {}
    for source, symbols, score, engagement in rows:
        family = str(source).split(":")[0][:24]
        for symbol in symbols or []:
            key = (str(symbol).upper()[:12], family)
            bucket = buckets.setdefault(
                key, {"mentions": 0, "scores": [], "engagement": 0}
            )
            bucket["mentions"] += 1
            bucket["scores"].append(float(score or 0.0))
            bucket["engagement"] += int(engagement or 0)

    for (symbol, family), bucket in buckets.items():
        stats = sentiment_engine.aggregate(bucket["scores"])
        existing = db.get(SocialMentionRow, (symbol, family, target))
        if existing is None:
            db.add(
                SocialMentionRow(
                    symbol=symbol,
                    source=family,
                    day=target,
                    mentions=bucket["mentions"],
                    sentiment=stats["mean"],
                    positive=stats["positive"],
                    negative=stats["negative"],
                    engagement=bucket["engagement"],
                )
            )
        else:
            existing.mentions = bucket["mentions"]
            existing.sentiment = stats["mean"]
            existing.positive = stats["positive"]
            existing.negative = stats["negative"]
            existing.engagement = bucket["engagement"]
            existing.updated_at = datetime.now(UTC)

    db.flush()
    return len(buckets)


def collect_all(
    db: Session,
    include_news: bool = True,
    include_government: bool = True,
    include_social: bool = True,
    include_etf_holdings: bool = True,
) -> CollectionResult:
    """One full collection pass across every free source.

    Each source group is wrapped independently: a hard failure in one never
    prevents the others from contributing, and the result records who
    answered so the report can state its own coverage honestly.
    """
    result = CollectionResult(as_of=datetime.now(UTC))
    allowed = known_symbols(db)
    items: list[FeedItem] = []

    if include_news:
        try:
            found, sources = news.collect()
            items.extend(found)
            result.sources_answered.extend(sources)
        except Exception:
            log.exception("trend collection failed", source="news")
            result.sources_failed.append("news")

    if include_government:
        try:
            found, sources = government.collect()
            items.extend(found)
            result.sources_answered.extend(sources)
        except Exception:
            log.exception("trend collection failed", source="government")
            result.sources_failed.append("government")

    if include_social:
        try:
            # Per-symbol streams matter as much as the trending list: a
            # trending entry is titled "$XYZ is trending", which carries no
            # theme vocabulary and so can never attach to a theme. The message
            # bodies from a symbol's own stream do. Without this the social
            # component is permanently unmeasurable even when StockTwits is up.
            found, sources = social.collect(symbols=social_focus_symbols(db))
            items.extend(found)
            result.sources_answered.extend(sources)
        except Exception:
            log.exception("trend collection failed", source="social")
            result.sources_failed.append("social")

    result.documents_seen = len(items)
    stored, by_channel = store_documents(db, items, allowed)
    result.documents_stored = stored
    result.by_channel = by_channel
    result.social_symbols = rebuild_social_aggregates(db)

    if include_etf_holdings:
        try:
            holdings, sources = etf_source.collect_holdings()
            result.holdings_rows = etf_source.store_holdings(db, holdings)
            result.etfs_with_holdings = sorted(holdings)
            result.sources_answered.extend(sources)
        except Exception:
            log.exception("trend collection failed", source="etf_holdings")
            result.sources_failed.append("etf_holdings")

    db.add(
        SystemEvent(
            kind="trends.collect",
            message=(
                f"trend collection: {result.documents_stored} new documents from "
                f"{len(result.sources_answered)} sources, "
                f"{len(result.etfs_with_holdings)} ETFs with published holdings "
                "— 0 LLM calls"
            ),
            payload={
                "documents_seen": result.documents_seen,
                "documents_stored": result.documents_stored,
                "by_channel": result.by_channel,
                "sources_failed": result.sources_failed,
                "etfs_with_holdings": result.etfs_with_holdings,
            },
        )
    )
    db.flush()
    log.info(
        "trend collection complete",
        seen=result.documents_seen,
        stored=result.documents_stored,
        sources=len(result.sources_answered),
    )
    return result


def purge_old_documents(db: Session, keep_days: int = 60) -> int:
    """Housekeeping for the nightly job. Documents are evidence, not history —
    once they fall out of every scoring window they are dead weight."""
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    stale = db.execute(
        select(TrendDocumentRow.doc_key).where(TrendDocumentRow.published_at < cutoff).limit(20_000)
    ).scalars().all()
    if not stale:
        return 0
    db.execute(delete(TrendDocumentRow).where(TrendDocumentRow.doc_key.in_(stale)))
    db.flush()
    return len(stale)
