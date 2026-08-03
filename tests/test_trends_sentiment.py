"""Sentiment lexicon: grammar handling and finance-specific vocabulary."""

from sentinel.trends import sentiment


def test_positive_and_negative_direction():
    assert sentiment.score_text("Company beats earnings, raises guidance") > 0.3
    assert sentiment.score_text("Company misses badly and cuts guidance") < -0.3


def test_neutral_text_scores_zero_with_no_hits():
    detail = sentiment.score_detail("The company scheduled a meeting for Tuesday")
    assert detail["compound"] == 0.0
    assert detail["hits"] == 0


def test_negation_flips_polarity():
    positive = sentiment.score_text("the outlook is strong")
    negated = sentiment.score_text("the outlook is not strong")
    assert positive > 0
    assert negated < 0


def test_negation_only_reaches_backwards():
    """Negation uses a backward window, as in VADER. A sentiment word BEFORE
    the negator keeps its own polarity — 'growth' in 'growth was not strong'
    is still about growth, and only 'strong' is negated."""
    both = sentiment.score_detail("revenue growth was not strong")
    assert both["positive"] >= 1  # 'growth' survived
    assert both["negative"] >= 1  # 'strong' was flipped


def test_intensifier_amplifies():
    plain = sentiment.score_text("results were weak")
    intense = sentiment.score_text("results were extremely weak")
    assert intense < plain


def test_dampener_reduces():
    plain = sentiment.score_text("shares declined")
    damped = sentiment.score_text("shares slightly declined")
    assert damped > plain  # less negative


def test_multiword_phrase_beats_component_words():
    """'going concern' must not be scored as a mildly negative 'concern'."""
    mild = sentiment.score_text("there is some concern")
    severe = sentiment.score_text("there is going concern doubt")
    assert severe < mild


def test_finance_vocabulary_is_not_generic():
    """Words a general-purpose lexicon gets wrong in a financial context."""
    # "contract award" is unambiguously good news for a defense name.
    assert sentiment.score_text("Company wins contract award from the Navy") > 0.2
    # Dilution is negative even though "offering" sounds neutral-to-positive.
    assert sentiment.score_text("dilutive offering announced") < 0


def test_all_caps_emphasis_ignores_tickers():
    """NVDA and FDA are upper-case by nature and must not act as emphasis."""
    with_ticker = sentiment.score_detail("NVDA beat estimates")
    plain = sentiment.score_detail("the company beat estimates")
    assert abs(with_ticker["compound"] - plain["compound"]) < 0.05


def test_all_caps_word_does_amplify():
    normal = sentiment.score_text("this is a disappointing quarter")
    shouted = sentiment.score_text("this is a DISAPPOINTING quarter")
    assert shouted < normal


def test_emoji_carry_retail_sentiment():
    assert sentiment.score_text("uranium \U0001F680\U0001F680") > 0
    assert sentiment.score_text("my calls \U0001F4A9") < 0


def test_scores_stay_bounded():
    extreme = "beat beat beat record surge soared upgraded breakthrough " * 10
    assert -1.0 <= sentiment.score_text(extreme) <= 1.0


def test_label_thresholds():
    assert sentiment.label(0.5) == "positive"
    assert sentiment.label(-0.5) == "negative"
    assert sentiment.label(0.0) == "neutral"


def test_aggregate_reports_split_not_just_mean():
    """Polarised discussion must be distinguishable from indifference."""
    polarised = sentiment.aggregate([0.8, -0.8, 0.7, -0.7])
    indifferent = sentiment.aggregate([0.0, 0.0, 0.0, 0.0])
    assert abs(polarised["mean"]) < 0.1
    assert abs(indifferent["mean"]) < 0.1
    assert polarised["positive"] == 2 and polarised["negative"] == 2
    assert indifferent["neutral"] == 4


def test_empty_input_is_safe():
    assert sentiment.score_text("") == 0.0
    assert sentiment.aggregate([])["count"] == 0
