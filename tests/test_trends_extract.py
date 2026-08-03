"""Theme matching and ticker extraction — the precision-critical stage.

A false ticker is worse than a missed one: it puts an unrelated company in
front of the user with a dollar amount attached.
"""

from sentinel.trends import extract
from sentinel.trends.taxonomy import THEMES_BY_KEY, seed_symbols, thematic_etfs

KNOWN = frozenset({"NVDA", "CCJ", "UEC", "LMT", "T", "ALL", "NOW", "KEY", "MU", "URA"})


# ------------------------------------------------------------- themes ----
def test_theme_matched_on_domain_phrase():
    themes = extract.extract_themes("Utility signs deal for a small modular reactor")
    assert "nuclear" in themes


def test_theme_not_matched_on_incidental_word():
    """'ai' as a bare substring must not fire on 'said', 'chain', 'plain'."""
    themes = extract.extract_themes("He said the supply chain remains plain and stable")
    assert "ai" not in themes


def test_multiple_themes_can_match():
    themes = extract.extract_themes(
        "Defense contract awarded for an AI datacenter serving military logistics"
    )
    assert "defense" in themes
    assert "ai" in themes


def test_keyword_hits_are_reported_for_evidence():
    hits = extract.theme_keyword_hits("Uranium price rises as enrichment capacity tightens")
    assert "uranium" in hits
    assert any("uranium" in kw for kw in hits["uranium"])


def test_no_theme_for_unrelated_text():
    assert extract.extract_themes("Retailer reports quarterly same-store sales") == []


# ------------------------------------------------------------ tickers ----
def test_cashtag_is_always_extracted():
    assert extract.extract_symbols("loading up on $UEC today", KNOWN) == ["UEC"]


def test_bare_known_symbol_extracted():
    assert "NVDA" in extract.extract_symbols("NVDA reported results", KNOWN)


def test_unknown_symbol_never_extracted():
    """Anything outside the allow-list cannot reach a recommendation."""
    assert extract.extract_symbols("ZZZZ soared today", KNOWN) == []
    assert extract.extract_symbols("$ZZZZ soared today", KNOWN) == []


def test_common_abbreviations_are_not_tickers():
    text = "The CEO told the SEC that EPS and GDP data support the ETF thesis"
    assert extract.extract_symbols(text, KNOWN) == []


def test_ambiguous_single_letter_needs_a_cashtag():
    """'T', 'ALL', 'NOW' and 'KEY' are real tickers AND ordinary words."""
    assert extract.extract_symbols("ALL of it is KEY right NOW", KNOWN) == []
    assert extract.extract_symbols("I bought $T and $ALL", KNOWN) == ["ALL", "T"]


def test_sentence_initial_word_is_not_a_ticker():
    assert extract.extract_symbols("Now the market is calm", KNOWN) == []


def test_company_name_matching(db):
    from tests.trend_synth import seed_fundamentals

    seed_fundamentals(db, "CCJ", name="Cameco Corporation")
    index = extract.build_name_index(db)
    found = extract.extract_symbols(
        "Cameco signed a long-term supply agreement", KNOWN, index
    )
    assert found == ["CCJ"]


def test_short_company_names_are_excluded_from_name_index(db):
    """A three-letter normalised name would match half the corpus."""
    from tests.trend_synth import seed_fundamentals

    seed_fundamentals(db, "NOW", name="Now Inc")
    index = extract.build_name_index(db)
    assert "now" not in index


def test_generic_company_names_are_excluded_from_name_index(db):
    """Length is not enough: 'Energy Transfer' normalises to 'transfer',
    which is long but appears in ordinary prose constantly."""
    from tests.trend_synth import seed_fundamentals

    seed_fundamentals(db, "ET", name="Energy Transfer LP")
    index = extract.build_name_index(db)
    assert "transfer" not in index


def test_ambiguous_company_name_is_dropped(db):
    """Two symbols normalising to the same name is a guess, not a match."""
    from tests.trend_synth import seed_fundamentals

    seed_fundamentals(db, "AAA", name="Atlantic Power Holdings")
    seed_fundamentals(db, "BBB", name="Atlantic Power Corporation")
    index = extract.build_name_index(db)
    assert "atlantic power" not in index


# ----------------------------------------------------------- taxonomy ----
def test_taxonomy_seeds_and_etfs_do_not_overlap():
    """ETFs are evidence, never stock picks — they must not appear as seeds."""
    assert set(seed_symbols()).isdisjoint(set(thematic_etfs()))


def test_every_theme_has_keywords_and_constituents():
    for theme in THEMES_BY_KEY.values():
        assert theme.keywords, f"{theme.key} has no keywords"
        assert theme.seeds or theme.etfs, f"{theme.key} has no constituents"
