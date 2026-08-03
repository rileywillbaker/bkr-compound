"""Free-source collectors: parsing, and the never-raise degradation contract.

Free endpoints fail constantly and unremarkably. The contract every collector
must honour is that a failure yields an empty result and a logged note, never
an exception into the caller.
"""

import httpx
import pytest

from sentinel.trends.sources import etf, feeds, government, news, social

RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example Finance</title>
    <item>
      <title>Utility signs small modular reactor deal</title>
      <description>&lt;p&gt;A &lt;b&gt;major&lt;/b&gt; agreement&lt;/p&gt;</description>
      <link>https://example.test/a</link>
      <pubDate>Tue, 15 Jul 2025 13:45:00 GMT</pubDate>
    </item>
    <item>
      <title>Uranium prices climb</title>
      <link>https://example.test/b</link>
      <pubDate>Tue, 15 Jul 2025 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Agency Newsroom</title>
  <entry>
    <title>NRC issues advanced reactor rule</title>
    <summary>Notice of final rulemaking</summary>
    <link rel="alternate" href="https://agency.test/rule"/>
    <published>2025-07-15T12:00:00Z</published>
  </entry>
</feed>
"""


# -------------------------------------------------------------- parsing ----
def test_parse_rss():
    items = feeds.parse_feed(RSS, source="example")
    assert len(items) == 2
    assert items[0].title == "Utility signs small modular reactor deal"
    assert items[0].summary == "A major agreement"  # tags stripped
    assert items[0].url == "https://example.test/a"
    assert items[0].published_at.year == 2025


def test_parse_atom():
    items = feeds.parse_feed(ATOM, source="agency", channel="gov")
    assert len(items) == 1
    assert items[0].channel == "gov"
    assert items[0].url == "https://agency.test/rule"


def test_parse_malformed_xml_returns_empty():
    assert feeds.parse_feed("<rss><channel><item>", source="broken") == []
    assert feeds.parse_feed("", source="empty") == []
    assert feeds.parse_feed("not xml at all", source="junk") == []


def test_items_without_titles_are_skipped():
    xml = '<?xml version="1.0"?><rss><channel><item><link>x</link></item></channel></rss>'
    assert feeds.parse_feed(xml, source="x") == []


def test_doc_key_is_stable_and_deduplicates():
    first = feeds.parse_feed(RSS, source="example")[0]
    second = feeds.parse_feed(RSS, source="example")[0]
    assert first.doc_key() == second.doc_key()
    other = feeds.parse_feed(RSS, source="different")[0]
    assert first.doc_key() != other.doc_key()


def test_timestamp_formats():
    assert feeds.parse_timestamp("Tue, 15 Jul 2025 13:45:00 GMT") is not None
    assert feeds.parse_timestamp("2025-07-15T12:00:00Z") is not None
    assert feeds.parse_timestamp("2025-07-15") is not None
    assert feeds.parse_timestamp("nonsense") is None
    assert feeds.parse_timestamp(None) is None


def test_clean_html_handles_entities():
    assert feeds.clean_html("<p>A &amp; B</p>") == "A & B"
    assert feeds.clean_html(None) == ""


# ---------------------------------------------------------- degradation ----
def test_fetch_returns_none_on_transport_error():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert feeds.fetch("https://example.test/x", client=client) is None


def test_fetch_returns_none_on_http_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    assert feeds.fetch("https://example.test/x", client=client) is None


def test_fetch_json_returns_none_on_non_json():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html/>"))
    )
    assert feeds.fetch_json("https://example.test/x", client=client) is None


def test_fetch_feed_degrades_to_empty_list():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert feeds.fetch_feed("https://example.test/x", source="x", client=client) == []


def test_post_json_degrades():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert feeds.post_json("https://example.test/x", {"a": 1}, client=client) is None


@pytest.mark.parametrize(
    "collector",
    [news.collect_broad, government.collect_agency_feeds, social.collect_reddit],
)
def test_collectors_never_raise_when_everything_fails(monkeypatch, collector):
    """The whole point: a dead internet produces an empty run, not a crash."""
    monkeypatch.setattr(feeds, "fetch", lambda *a, **k: None)
    monkeypatch.setattr("sentinel.trends.sources.feeds.fetch", lambda *a, **k: None)
    monkeypatch.setattr("sentinel.trends.sources.feeds.fetch_json", lambda *a, **k: None)
    items, sources = collector()
    assert items == []
    assert sources == []


def test_social_focus_falls_back_on_a_cold_install(db):
    """First run has no snapshots, but per-symbol social calls must still be
    aimed somewhere or the social component can never be measured."""
    from sentinel.trends.collect import social_focus_symbols

    focus = social_focus_symbols(db, limit=12)
    assert 0 < len(focus) <= 12
    assert len(focus) == len(set(focus))


def test_social_focus_round_robins_across_themes(db):
    """A single dominant theme must not consume the whole throttled budget."""
    from datetime import date

    from sentinel.db.models import TrendSnapshotRow
    from sentinel.trends.collect import social_focus_symbols

    db.add(
        TrendSnapshotRow(
            theme="uranium", day=date.today(), score=90.0, symbols=["CCJ", "UEC", "DNN", "NXE"]
        )
    )
    db.add(
        TrendSnapshotRow(
            theme="defense", day=date.today(), score=80.0, symbols=["LMT", "RTX", "NOC"]
        )
    )
    db.flush()

    focus = social_focus_symbols(db, limit=4)
    assert "CCJ" in focus and "LMT" in focus  # both themes represented
    assert len(focus) == len(set(focus))


def test_x_twitter_is_explicitly_unavailable():
    """No free read tier exists; the collector must report the gap, not fake it."""
    items, sources = social.collect_x()
    assert items == []
    assert sources == []


# ---------------------------------------------------------- ETF holdings ----
ARK_CSV = """date,fund,company,ticker,cusip,shares,"market value ($)","weight (%)"
07/15/2025,ARKQ,TESLA INC,TSLA,88160R101,"100,000","$25,000,000",10.50
07/15/2025,ARKQ,NVIDIA CORP,NVDA,67066G104,"50,000","$5,000,000",4.25
07/15/2025,ARKQ,,,,,,
"""

ISHARES_CSV = """iShares U.S. Aerospace & Defense ETF
Fund Holdings as of,"Jul 15, 2025"
Inception Date,"May 01, 2006"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Shares
LMT,LOCKHEED MARTIN CORP,Industrials,Equity,"$1,000,000",8.50,"2,000"
RTX,RTX CORP,Industrials,Equity,"$900,000",7.75,"9,000"
XTSLA,BLK CSH FND TREASURY,Cash,Cash,"$5,000",0.04,"5,000"
"""


def test_parse_ark_holdings():
    records = etf.parse_holdings_csv(ARK_CSV, "ARKQ")
    symbols = {r.symbol for r in records}
    assert symbols == {"TSLA", "NVDA"}
    tesla = next(r for r in records if r.symbol == "TSLA")
    assert tesla.weight_pct == 10.50
    assert tesla.shares == 100_000
    assert tesla.market_value == 25_000_000


def test_parse_ishares_holdings_skips_metadata_preamble():
    """iShares prefixes several lines before the real header row."""
    records = etf.parse_holdings_csv(ISHARES_CSV, "ITA")
    symbols = {r.symbol for r in records}
    assert "LMT" in symbols
    assert "RTX" in symbols
    lmt = next(r for r in records if r.symbol == "LMT")
    assert lmt.weight_pct == 8.50
    assert lmt.name == "LOCKHEED MARTIN CORP"


def test_parse_holdings_rejects_unrecognisable_file():
    assert etf.parse_holdings_csv("a,b,c\n1,2,3", "XXX") == []
    assert etf.parse_holdings_csv("", "XXX") == []


def test_store_and_diff_holdings(db):
    from datetime import date, timedelta

    from sentinel.trends.scoring import etf_accumulation

    records = etf.parse_holdings_csv(ARK_CSV, "ARKQ")
    etf.store_holdings(db, {"ARKQ": records}, as_of=date.today() - timedelta(days=30))

    # Same file a month later with NVDA's weight raised.
    grown = etf.parse_holdings_csv(ARK_CSV.replace("4.25", "8.90"), "ARKQ")
    etf.store_holdings(db, {"ARKQ": grown}, as_of=date.today())

    accumulation = etf_accumulation(db, ["ARKQ"])
    increased = {a.symbol for a in accumulation}
    assert "NVDA" in increased
    nvda = next(a for a in accumulation if a.symbol == "NVDA")
    assert nvda.weight_change == pytest.approx(4.65, abs=0.01)


def test_store_holdings_is_idempotent_within_a_day(db):
    from datetime import date

    from sentinel.db.models import EtfHoldingRow

    records = etf.parse_holdings_csv(ARK_CSV, "ARKQ")
    etf.store_holdings(db, {"ARKQ": records}, as_of=date.today())
    etf.store_holdings(db, {"ARKQ": records}, as_of=date.today())
    assert db.query(EtfHoldingRow).count() == 2  # TSLA + NVDA, not 4


def test_tracked_etfs_are_not_empty():
    assert len(etf.tracked_etfs()) > 10


# --------------------------------------------- per-issuer retrieval ----
def _endpoint(issuer: str, **kw) -> etf.HoldingsEndpoint:
    return etf.HoldingsEndpoint(etf=kw.pop("etf", "ITA"), issuer=issuer, **kw)


def test_ark_uses_the_direct_url(monkeypatch):
    seen = []

    def fake_fetch(url, provider="rss", **kw):
        seen.append(url)
        return ARK_CSV

    monkeypatch.setattr(etf, "fetch", fake_fetch)
    records = etf.fetch_holdings(_endpoint("ark", etf="ARKQ", url="https://ark.test/a.csv"))
    assert [r.symbol for r in records] == ["TSLA", "NVDA"]
    assert seen == ["https://ark.test/a.csv"]


def test_ishares_stops_at_the_first_populated_slug(monkeypatch):
    """The slug is part of the CDN cache key and some entries hold an empty
    file, so a populated one has to be found — but only until it is."""
    calls = []

    def fake_fetch(url, provider="rss", **kw):
        calls.append(url)
        # First spelling returns the header-only file, second the real one.
        return ISHARES_CSV if len(calls) >= 2 else "Fund Holdings as of,Jul 15\n"

    monkeypatch.setattr(etf, "fetch", fake_fetch)
    records = etf.fetch_holdings(_endpoint("ishares", etf="ITA", product_id="239502"))
    assert {r.symbol for r in records} >= {"LMT", "RTX"}
    assert len(calls) == 2  # stopped as soon as it worked
    assert all("239502" in url and "latest-holdings.csv" in url for url in calls)


def test_ishares_gives_up_after_every_slug(monkeypatch):
    monkeypatch.setattr(etf, "fetch", lambda url, provider="rss", **kw: "junk,header\n1,2\n")
    assert etf.fetch_holdings(_endpoint("ishares", product_id="239502")) == []


def test_globalx_follows_the_dated_cdn_link(monkeypatch):
    """The CDN filename embeds the holdings date, so it cannot be constructed
    and must be read off the fund page."""
    csv_url = "https://assets.globalxetfs.com/funds/holdings/ura_full-holdings_20260731.csv"
    page = f'<a href="{csv_url}"><button>Full Holdings (.csv)</button></a>'
    seen = []

    def fake_fetch(url, provider="rss", **kw):
        seen.append(url)
        # The CDN host also contains "/funds/", so discriminate on the host.
        return ARK_CSV if url.startswith("https://assets.") else page

    monkeypatch.setattr(etf, "fetch", fake_fetch)
    records = etf.fetch_holdings(_endpoint("globalx", etf="URA"))
    assert [r.symbol for r in records] == ["TSLA", "NVDA"]
    assert seen[0].endswith("/funds/ura/")
    assert seen[1] == csv_url


def test_globalx_degrades_when_the_page_has_no_link(monkeypatch):
    monkeypatch.setattr(etf, "fetch", lambda url, provider="rss", **kw: "<html>no link</html>")
    assert etf.fetch_holdings(_endpoint("globalx", etf="URA")) == []


def test_fetch_holdings_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("issuer exploded")

    monkeypatch.setattr(etf, "fetch", boom)
    for issuer in ("ark", "ishares", "globalx"):
        assert etf.fetch_holdings(_endpoint(issuer, product_id="1", url="https://x.test")) == []


def test_every_endpoint_declares_what_it_needs():
    """A misconfigured row would silently fetch nothing forever."""
    for endpoint in etf.HOLDINGS_ENDPOINTS:
        assert endpoint.issuer in etf._FETCHERS, endpoint.etf
        if endpoint.issuer == "ark":
            assert endpoint.url, endpoint.etf
        if endpoint.issuer == "ishares":
            assert endpoint.product_id.isdigit(), endpoint.etf
