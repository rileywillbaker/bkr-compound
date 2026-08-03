"""Government, regulatory and federal-spending signals — all free and keyless.

Policy is the slowest-moving and most under-priced input a retail investor
has access to. A rule enters the Federal Register weeks before the trade press
writes it up, and a contract award appears in USAspending before it appears in
an earnings call. None of it costs anything.

Sources, and why each is here:

  **Federal Register API** (`federalregister.gov/api/v1`) — the government's
  own JSON API for every proposed rule, final rule, notice and presidential
  document. Free, documented, no key, generous limits. This is the single
  best free policy signal available and the backbone of `policy_support`.

  **USAspending API** (`api.usaspending.gov`) — every federal award, by
  recipient and amount. Free, documented, no key. Used to see which listed
  companies are actually receiving defense and energy money, rather than
  which ones are talked about.

  **Agency newsrooms** — DoD contract announcements, Department of Energy,
  NRC, and White House presidential actions, via each site's public feed.
  These are the least reliable of the three (agencies reshape their CMS
  regularly), so each is optional and additive.

Everything degrades independently: with all of it unavailable, the policy
component of a trend score is simply reported as having no coverage rather
than as a zero, which matters because "no policy news" and "we could not
check for policy news" are very different claims.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta

import structlog

from sentinel.trends.sources.feeds import FeedItem, fetch_feed, fetch_json, post_json
from sentinel.trends.taxonomy import THEMES, Theme

log = structlog.get_logger()

FEDERAL_REGISTER_URL = "https://www.federalregister.gov/api/v1/documents.json"
USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# Agency feeds. Each is best-effort; see the module docstring.
AGENCY_FEEDS: tuple[tuple[str, str], ...] = (
    ("whitehouse_actions", "https://www.whitehouse.gov/presidential-actions/feed/"),
    ("doe_articles", "https://www.energy.gov/articles/feed"),
    ("nrc_news", "https://www.nrc.gov/public-involve/listserver/rss.xml"),
    ("dod_releases", "https://www.defense.gov/DesktopModules/ArticleCSS/RSS.ashx?ContentType=1&Site=945&max=25"),
    ("dod_contracts", "https://www.defense.gov/DesktopModules/ArticleCSS/RSS.ashx?ContentType=800&Site=945&max=25"),
)

# Award type codes for contracts (A-D) — grants and loans are excluded because
# a grant to a university is not a signal about a listed company.
_CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]

# Toptier agencies whose spending is thematically meaningful and mappable.
_SPENDING_AGENCIES: tuple[tuple[str, str], ...] = (
    ("defense", "Department of Defense"),
    ("energy", "Department of Energy"),
)


def collect_federal_register(
    themes: tuple[Theme, ...] = THEMES,
    days: int = 7,
    per_query: int = 20,
) -> tuple[list[FeedItem], list[str]]:
    """Recent Federal Register documents matching each theme's gov queries.

    A 7-day window (rather than the 2 days used for news) is deliberate:
    rulemaking is slow, and a rule published on Monday is still the live
    policy fact on Friday.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    items: list[FeedItem] = []
    answered: list[str] = []

    for theme in themes:
        for query in theme.gov_queries:
            payload = fetch_json(
                FEDERAL_REGISTER_URL,
                provider="federalregister",
                params={
                    "conditions[term]": query,
                    "conditions[publication_date][gte]": since,
                    "per_page": per_query,
                    "order": "newest",
                    "fields[]": [
                        "title",
                        "abstract",
                        "html_url",
                        "publication_date",
                        "type",
                        "agencies",
                        "document_number",
                    ],
                },
            )
            if not isinstance(payload, dict):
                continue
            results = payload.get("results")
            if not isinstance(results, list):
                continue
            answered.append(f"federal_register:{theme.key}:{query}")
            for doc in results:
                if not isinstance(doc, dict):
                    continue
                published = doc.get("publication_date")
                try:
                    stamp = (
                        datetime.fromisoformat(str(published)).replace(tzinfo=UTC)
                        if published
                        else datetime.now(UTC)
                    )
                except ValueError:
                    stamp = datetime.now(UTC)
                agencies = doc.get("agencies") or []
                agency_names = ", ".join(
                    str(a.get("name", "")) for a in agencies if isinstance(a, dict)
                )
                items.append(
                    FeedItem(
                        source="federal_register",
                        channel="gov",
                        title=str(doc.get("title") or "")[:600],
                        summary=str(doc.get("abstract") or "")[:2000],
                        url=str(doc.get("html_url") or "")[:1000],
                        author=agency_names[:128],
                        published_at=stamp,
                        extra={
                            "theme_hint": theme.key,
                            "doc_type": doc.get("type", ""),
                            "query": query,
                        },
                    )
                )
    log.info("federal register collected", items=len(items), queries=len(answered))
    return items, answered


def collect_agency_feeds() -> tuple[list[FeedItem], list[str]]:
    """DoD / DOE / NRC / White House public feeds. Best effort, additive."""
    items: list[FeedItem] = []
    answered: list[str] = []
    for source, url in AGENCY_FEEDS:
        found = fetch_feed(url, source=source, channel="gov")
        if found:
            answered.append(source)
            items.extend(found[:40])
    log.info("agency feeds collected", items=len(items), sources=len(answered))
    return items, answered


def collect_federal_awards(
    days: int = 14, limit: int = 100
) -> tuple[list[FeedItem], list[str]]:
    """Recent large federal contract awards, by recipient, from USAspending.

    Each award becomes a document whose title names the recipient, so the
    normal ticker-extraction path maps it to a symbol when the recipient is
    a listed company (or a subsidiary whose name matches). Awards to private
    firms simply extract to no symbol and still count as theme evidence.
    """
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    items: list[FeedItem] = []
    answered: list[str] = []

    for theme_hint, agency_name in _SPENDING_AGENCIES:
        payload = post_json(
            USASPENDING_AWARDS_URL,
            {
                "filters": {
                    "time_period": [{"start_date": start, "end_date": end}],
                    "award_type_codes": _CONTRACT_AWARD_TYPES,
                    "agencies": [
                        {"type": "awarding", "tier": "toptier", "name": agency_name}
                    ],
                },
                "fields": [
                    "Award ID",
                    "Recipient Name",
                    "Award Amount",
                    "Description",
                    "Start Date",
                    "Awarding Agency",
                ],
                "sort": "Award Amount",
                "order": "desc",
                "limit": limit,
                "page": 1,
            },
            provider="usaspending",
        )
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        answered.append(f"usaspending:{theme_hint}")
        for award in results:
            if not isinstance(award, dict):
                continue
            recipient = str(award.get("Recipient Name") or "").strip()
            amount = award.get("Award Amount")
            if not recipient:
                continue
            try:
                amount_value = float(amount) if amount is not None else 0.0
            except (TypeError, ValueError):
                amount_value = 0.0
            stamp = datetime.now(UTC)
            raw_start = award.get("Start Date")
            if raw_start:
                with contextlib.suppress(ValueError):
                    stamp = datetime.fromisoformat(str(raw_start)).replace(tzinfo=UTC)
            items.append(
                FeedItem(
                    source="usaspending",
                    channel="gov",
                    title=(
                        f"{agency_name} awarded ${amount_value:,.0f} to {recipient}"
                    )[:600],
                    summary=str(award.get("Description") or "")[:2000],
                    url=(
                        "https://www.usaspending.gov/award/"
                        f"{award.get('generated_internal_id', '')}"
                    )[:1000],
                    author=agency_name[:128],
                    published_at=stamp,
                    extra={
                        "theme_hint": theme_hint,
                        "recipient": recipient,
                        "amount": amount_value,
                    },
                )
            )
    log.info("federal awards collected", items=len(items), agencies=len(answered))
    return items, answered


def collect(days: int = 7) -> tuple[list[FeedItem], list[str]]:
    """Everything in this module."""
    register_items, register_sources = collect_federal_register(days=days)
    agency_items, agency_sources = collect_agency_feeds()
    award_items, award_sources = collect_federal_awards()
    return (
        register_items + agency_items + award_items,
        register_sources + agency_sources + award_sources,
    )
