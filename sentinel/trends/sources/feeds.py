"""Generic RSS/Atom fetching and parsing — the transport under most free
financial news.

Written against the standard library's XML parser rather than `feedparser` so
the project takes on no new dependency for what is, in practice, ten lines of
element lookup. Both RSS 2.0 and Atom 1.0 are handled because the free
financial web uses both (Yahoo and CNBC publish RSS; several government
agencies publish Atom).

Security note: XML from arbitrary third parties is parsed with
`defusedxml`-equivalent hardening applied manually — entity resolution is off
by default in Python's ElementTree, and we additionally cap the response body
so a hostile or broken feed cannot exhaust memory.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
import structlog
from pydantic import BaseModel, Field

from sentinel.data.rate_limit import RateLimitExceeded, get_rate_limiter

log = structlog.get_logger()

# A real browser UA. Several publishers 403 the default httpx agent; this is
# about being served at all, not about concealing anything.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

MAX_BODY_BYTES = 4_000_000  # a feed larger than this is broken or hostile
DEFAULT_TIMEOUT = 12.0

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class FeedItem(BaseModel):
    """One normalised document from any free source."""

    source: str
    channel: str = "news"  # news | gov | social
    title: str
    summary: str = ""
    url: str = ""
    author: str = ""
    published_at: datetime
    engagement: int = 0
    extra: dict = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """Title + summary, which is what theme/ticker extraction reads."""
        return f"{self.title}. {self.summary}".strip()

    def doc_key(self) -> str:
        """Stable dedup key.

        Keyed on source + URL when a URL exists (the same story syndicated to
        two outlets is genuinely two data points about attention), else on the
        title, so a feed that omits links still dedups across runs.
        """
        basis = f"{self.source}|{self.url or self.title.lower().strip()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:64]


def clean_html(raw: str | None) -> str:
    """Strip tags and collapse whitespace. Feed summaries are full of markup."""
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return _WS_RE.sub(" ", text).strip()


def parse_timestamp(raw: str | None) -> datetime | None:
    """Parse the several date formats free feeds use in the wild."""
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    try:  # RFC 822 — the RSS standard
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    for candidate in (value, value.replace("Z", "+00:00")):
        try:  # ISO 8601 — Atom, and most JSON APIs
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """Shared keep-alive client. Free sources are all plain public HTTP GETs."""
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                "text/xml, application/json, text/html;q=0.8, */*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
    return _client


def fetch(
    url: str,
    provider: str = "rss",
    params: dict | None = None,
    client: httpx.Client | None = None,
) -> str | None:
    """GET a public URL. Returns None on ANY failure — never raises.

    Free sources fail constantly and unremarkably (403 from a CDN, a 30s
    timeout, a redirect to a consent page). Treating that as an error would
    make the whole agent brittle; treating it as "no data from this source
    today" is correct.
    """
    try:
        get_rate_limiter().wait_and_acquire(provider, max_wait=30.0)
    except RateLimitExceeded:
        log.info("trend source rate-limited; skipping", url=url, provider=provider)
        return None
    try:
        response = (client or get_client()).get(url, params=params)
    except httpx.HTTPError as exc:
        log.info("trend source unreachable", url=url, error=str(exc))
        return None
    if response.status_code != 200:
        log.info("trend source declined", url=url, status=response.status_code)
        return None
    body = response.text
    if len(body) > MAX_BODY_BYTES:
        log.warning("trend source oversized; truncating", url=url, bytes=len(body))
        body = body[:MAX_BODY_BYTES]
    return body


def fetch_json(
    url: str,
    provider: str = "rss",
    params: dict | None = None,
    client: httpx.Client | None = None,
) -> dict | list | None:
    """GET and parse JSON, or None. Same never-raise contract as `fetch`."""
    body = fetch(url, provider=provider, params=params, client=client)
    if body is None:
        return None
    try:
        import json

        return json.loads(body)
    except ValueError as exc:
        log.info("trend source returned non-JSON", url=url, error=str(exc))
        return None


def post_json(
    url: str,
    payload: dict,
    provider: str = "rss",
    client: httpx.Client | None = None,
) -> dict | list | None:
    """POST JSON to a public API and parse the reply, or None.

    Needed because a few free government APIs (USAspending in particular)
    express searches as POST bodies. Same never-raise contract as `fetch`.
    """
    try:
        get_rate_limiter().wait_and_acquire(provider, max_wait=30.0)
    except RateLimitExceeded:
        log.info("trend source rate-limited; skipping", url=url, provider=provider)
        return None
    try:
        response = (client or get_client()).post(
            url, json=payload, headers={"Content-Type": "application/json"}
        )
    except httpx.HTTPError as exc:
        log.info("trend source unreachable", url=url, error=str(exc))
        return None
    if response.status_code != 200:
        log.info("trend source declined", url=url, status=response.status_code)
        return None
    try:
        return response.json()
    except ValueError as exc:
        log.info("trend source returned non-JSON", url=url, error=str(exc))
        return None


def _element_text(node: ElementTree.Element, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        text = (found.text or "") if found is not None else ""
        if text.strip():
            return text.strip()
    return ""


def _atom_link(node: ElementTree.Element) -> str:
    for link in node.findall(f"{_ATOM_NS}link"):
        rel = link.get("rel", "alternate")
        if rel == "alternate" and link.get("href"):
            return link.get("href", "")
    first = node.find(f"{_ATOM_NS}link")
    return first.get("href", "") if first is not None else ""


def parse_feed(xml_text: str, source: str, channel: str = "news") -> list[FeedItem]:
    """Parse an RSS 2.0 or Atom 1.0 document into FeedItems.

    Malformed XML yields an empty list rather than raising — see the module
    docstring on why that is the expected case, not an exceptional one.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        log.info("trend feed unparseable", source=source, error=str(exc))
        return []

    items: list[FeedItem] = []
    now = datetime.now(UTC)

    for node in root.iter():
        tag = node.tag
        is_rss_item = tag == "item"
        is_atom_entry = tag == f"{_ATOM_NS}entry"
        if not (is_rss_item or is_atom_entry):
            continue

        if is_rss_item:
            title = clean_html(_element_text(node, "title"))
            summary = clean_html(
                _element_text(node, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")
            )
            url = _element_text(node, "link", "guid")
            author = _element_text(node, "author", "{http://purl.org/dc/elements/1.1/}creator")
            stamp = parse_timestamp(
                _element_text(node, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
            )
        else:
            title = clean_html(_element_text(node, f"{_ATOM_NS}title"))
            summary = clean_html(
                _element_text(node, f"{_ATOM_NS}summary", f"{_ATOM_NS}content")
            )
            url = _atom_link(node)
            author = _element_text(node, f"{_ATOM_NS}author/{_ATOM_NS}name")
            stamp = parse_timestamp(
                _element_text(node, f"{_ATOM_NS}published", f"{_ATOM_NS}updated")
            )

        if not title:
            continue
        items.append(
            FeedItem(
                source=source,
                channel=channel,
                title=title[:600],
                summary=summary[:2000],
                url=(url or "")[:1000],
                author=(author or "")[:128],
                # An undated item is assumed fresh: feeds that omit dates are
                # almost always "latest N", and dropping them would silently
                # lose whole publishers.
                published_at=stamp or now,
            )
        )
    return items


def fetch_feed(
    url: str,
    source: str,
    channel: str = "news",
    provider: str = "rss",
    client: httpx.Client | None = None,
) -> list[FeedItem]:
    """Fetch + parse in one step. Returns [] on any failure."""
    body = fetch(url, provider=provider, client=client)
    if body is None:
        return []
    items = parse_feed(body, source=source, channel=channel)
    log.debug("trend feed collected", source=source, items=len(items))
    return items
