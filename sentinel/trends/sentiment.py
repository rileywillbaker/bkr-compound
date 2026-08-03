"""Finance-tuned sentiment scoring — free, offline, zero dependencies.

Why a lexicon and not a transformer
-----------------------------------
The requirement is free operation. A hosted sentiment API costs money; a local
transformer (FinBERT and friends) is free to *use* but drags in torch — a
multi-gigabyte dependency to score a few thousand short headlines a day, on a
machine that also sleeps most of the day. A lexicon scorer runs in
microseconds, has no install story, and is fully deterministic, which matters
here because sentiment feeds a *score the user sees* and must be reproducible.

The approach combines two well-known open methods, implemented natively:

  * VADER's grammar handling — negation windows, intensifiers/dampeners,
    ALL-CAPS emphasis, punctuation emphasis, and the same compound-score
    normalisation x / sqrt(x² + α).
  * Loughran-McDonald's insight that general-purpose sentiment lexicons are
    wrong for finance. "Liability", "tax" and "crude" are not negative in a
    filing; "beat", "guidance raise" and "backlog" carry meaning that generic
    lexicons miss entirely. The word list below is finance-first.

Retail-forum vocabulary (rocket emoji, "bagholder", "to the moon") is included
deliberately: it is a strong signal, but of ATTENTION rather than of quality.
`scoring.py` treats social sentiment as the component most likely to indicate
hype, and caps its contribution accordingly.

Everything here is pure functions over strings. No I/O, no network, no state.
"""

import math
import re

# Valences run -3..+3 in VADER's convention; kept here for the same feel.
_LEXICON: dict[str, float] = {
    # --- results and guidance -------------------------------------------
    "beat": 2.0, "beats": 2.0, "outperform": 2.0, "outperformed": 2.0,
    "exceeded": 1.9, "exceeds": 1.9, "surpassed": 1.9, "topped": 1.6,
    "record": 1.8, "strong": 1.7, "robust": 1.6, "solid": 1.2,
    "accelerating": 1.8, "accelerated": 1.6, "momentum": 1.2,
    "raised": 1.9, "raises": 1.9, "upgraded": 2.2, "upgrade": 2.2,
    "growth": 1.4, "expansion": 1.3, "expanding": 1.3, "surge": 2.0,
    "surged": 2.0, "soared": 2.3, "soars": 2.3, "rally": 1.7, "rallied": 1.7,
    "jumped": 1.7, "climbed": 1.3, "gains": 1.3, "gained": 1.3,
    "profitable": 1.8, "profit": 1.2, "profits": 1.2, "margin": 0.4,
    "backlog": 1.2, "bookings": 1.0, "demand": 1.0, "orders": 0.9,
    "breakthrough": 2.2, "milestone": 1.4, "approval": 1.8, "approved": 1.8,
    "awarded": 1.9, "award": 1.6, "wins": 1.8, "won": 1.5, "secured": 1.5,
    "contract": 0.9, "partnership": 1.2, "collaboration": 1.0,
    "expansion plans": 1.2, "buyback": 1.5, "repurchase": 1.4,
    "dividend increase": 1.6, "guidance raise": 2.2, "outperformance": 1.8,
    "tailwind": 1.6, "tailwinds": 1.6, "catalyst": 1.1, "upside": 1.5,
    "bullish": 2.0, "optimistic": 1.5, "confident": 1.2, "opportunity": 1.0,
    "innovative": 1.1, "leading": 1.0, "leader": 1.1, "dominant": 1.3,
    "efficient": 0.9, "recovery": 1.3, "rebound": 1.5, "turnaround": 1.4,
    "undervalued": 1.6, "cheap": 0.8, "oversold": 0.9,
    # --- deterioration ---------------------------------------------------
    "miss": -2.0, "missed": -2.0, "misses": -2.0, "shortfall": -2.0,
    "disappointing": -2.1, "disappointed": -1.9, "disappoints": -2.0,
    "weak": -1.8, "weakness": -1.8, "weaker": -1.7, "soft": -1.2,
    "declining": -1.7, "decline": -1.5, "declined": -1.5, "slowdown": -1.9,
    "slowing": -1.7, "deceleration": -1.8, "contraction": -1.8,
    "cut": -1.7, "cuts": -1.7, "slashed": -2.2, "lowered": -1.8,
    "downgraded": -2.2, "downgrade": -2.2, "reduced": -1.2,
    "plunge": -2.4, "plunged": -2.4, "plummeted": -2.5, "tumbled": -2.0,
    "crashed": -2.6, "crash": -2.4, "slumped": -1.9, "sank": -1.9,
    "fell": -1.2, "falls": -1.2, "dropped": -1.4, "sliding": -1.5,
    "loss": -1.6, "losses": -1.6, "unprofitable": -2.0, "deficit": -1.4,
    "warning": -2.0, "warns": -2.0, "warned": -2.0, "concern": -1.3,
    "concerns": -1.3, "risk": -0.8, "risks": -0.8, "uncertainty": -1.4,
    "headwind": -1.7, "headwinds": -1.7, "pressure": -1.2, "pressured": -1.2,
    "bankruptcy": -3.0, "insolvency": -3.0, "default": -2.6, "delisting": -2.8,
    "going concern": -2.9, "restatement": -2.6, "fraud": -3.0,
    "investigation": -2.2, "probe": -2.0, "subpoena": -2.2, "lawsuit": -1.8,
    "litigation": -1.6, "settlement": -0.8, "fine": -1.4, "penalty": -1.6,
    "recall": -2.1, "halted": -2.3, "suspension": -2.0, "suspended": -2.0,
    "layoffs": -1.6, "restructuring": -1.2, "writedown": -2.0,
    "impairment": -1.9, "dilution": -1.9, "offering": -1.2, "dilutive": -2.0,
    "bearish": -2.0, "overvalued": -1.6, "overbought": -0.9, "bubble": -1.8,
    "downside": -1.5, "underperform": -1.9, "underperformed": -1.9,
    "shorted": -1.2, "short seller": -2.0, "short report": -2.4,
    "delay": -1.5, "delayed": -1.5, "setback": -1.9, "rejected": -2.2,
    "denial": -2.0, "denied": -1.8, "sanctions": -1.6, "ban": -1.9,
    "restriction": -1.4, "restrictions": -1.4, "tariff": -1.2,
    # --- policy / government tone ---------------------------------------
    "funding": 1.4, "funded": 1.4, "subsidy": 1.4, "grant": 1.3,
    "incentive": 1.2, "authorization": 1.0, "appropriation": 1.2,
    "streamlined": 1.2, "deregulation": 1.1, "permitting reform": 1.5,
    "mandate": 0.8, "moratorium": -1.6, "repeal": -1.0, "phaseout": -1.4,
    "clawback": -1.7, "audit": -0.8,
    # --- retail-forum vocabulary (attention, not quality) ----------------
    "moon": 1.8, "mooning": 2.0, "squeeze": 1.6, "yolo": 1.0, "tendies": 1.6,
    "printing": 1.4, "loaded": 1.0, "buying": 0.8, "holding": 0.5,
    "diamond hands": 1.6, "bagholder": -1.9, "bagholding": -1.9,
    "dumping": -1.9, "dumped": -1.8, "rugged": -2.4, "rugpull": -2.6,
    "pump": -0.9, "pumped": -1.0, "scam": -2.8, "shill": -1.8,
    "overhyped": -2.0, "hype": -0.8, "dead": -1.8, "puts": -0.9, "calls": 0.7,
}

_EMOJI: dict[str, float] = {
    "\U0001F680": 1.8,  # rocket
    "\U0001F48E": 1.4,  # gem
    "\U0001F319": 1.2,  # crescent moon
    "\U0001F4C8": 1.3,  # chart increasing
    "\U0001F4C9": -1.3,  # chart decreasing
    "\U0001F525": 1.2,  # fire
    "\U0001FAE0": -1.0,  # melting face
    "\U0001F9FB": -1.4,  # roll of paper (worthless)
    "\U0001F4A9": -2.0,  # pile of poo
    "\U0001F440": 0.6,  # eyes
    "\U0001F62D": -1.4,  # loudly crying
    "\U0001FA82": 1.4,  # parachute
    "\U0001F43B": -1.4,  # bear
    "\U0001F402": 1.4,  # ox / bull
}

# Multi-word phrases are checked before tokenisation so "going concern" is not
# scored as a mildly positive "concern".
_PHRASES: tuple[tuple[str, float], ...] = tuple(
    sorted(
        ((k, v) for k, v in _LEXICON.items() if " " in k),
        key=lambda kv: -len(kv[0]),
    )
)

_NEGATIONS: frozenset[str] = frozenset({
    "not", "no", "never", "none", "cannot", "cant", "can't", "won't", "wont",
    "didn't", "didnt", "doesn't", "doesnt", "isn't", "isnt", "aren't", "arent",
    "wasn't", "wasnt", "weren't", "werent", "shouldn't", "shouldnt",
    "wouldn't", "wouldnt", "without", "lacks", "lacking", "fails", "failed",
    "failing", "unable", "hardly", "barely", "rarely", "neither", "nor",
    "despite", "although", "though",
})

_INTENSIFIERS: dict[str, float] = {
    "very": 0.3, "extremely": 0.45, "massively": 0.45, "hugely": 0.4,
    "significantly": 0.3, "substantially": 0.3, "sharply": 0.35,
    "dramatically": 0.4, "remarkably": 0.35, "exceptionally": 0.4,
    "record": 0.3, "unprecedented": 0.45, "major": 0.25, "strongly": 0.3,
    "deeply": 0.3, "highly": 0.25, "far": 0.2, "well": 0.15,
    "slightly": -0.3, "marginally": -0.35, "modestly": -0.25,
    "somewhat": -0.25, "slight": -0.3, "mildly": -0.3, "partially": -0.2,
    "barely": -0.35, "narrowly": -0.2,
}

_NEGATION_WINDOW = 3
_NEGATION_DAMPING = -0.68  # VADER's empirical flip factor
_CAPS_BOOST = 0.75
_EXCLAMATION_BOOST = 0.28
_MAX_EXCLAMATIONS = 3
_NORMALISATION_ALPHA = 15.0

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*|\d+(?:\.\d+)?%?")
_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    pattern = _WORD_BOUNDARY_CACHE.get(phrase)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
        _WORD_BOUNDARY_CACHE[phrase] = pattern
    return pattern


def _is_shouting(token: str) -> bool:
    """ALL-CAPS emphasis, but only for real words — tickers and acronyms
    ("NVDA", "FDA", "SMR") are upper-case by nature and mean nothing here."""
    return len(token) > 3 and token.isupper() and token.isalpha()


def score_text(text: str) -> float:
    """Compound sentiment for one document, in [-1, +1].

    0.0 means genuinely neutral OR no lexicon hit at all; callers that need to
    distinguish those should use `score_detail`.
    """
    return score_detail(text)["compound"]


def score_detail(text: str) -> dict:
    """Compound score plus the diagnostics scoring/reporting needs.

    Returns `hits` (how many lexicon terms fired) so a document with no
    recognised vocabulary can be excluded from sentiment averages rather than
    silently dragging them toward zero.
    """
    if not text or not text.strip():
        return {"compound": 0.0, "hits": 0, "positive": 0, "negative": 0}

    raw = text.strip()
    working = raw
    valences: list[float] = []

    # 1. Multi-word phrases first, then blank them out so their component
    #    words are not double-counted below.
    for phrase, valence in _PHRASES:
        pattern = _phrase_pattern(phrase)
        found = pattern.findall(working)
        if found:
            valences.extend([valence] * len(found))
            working = pattern.sub(" ", working)

    # 2. Emoji (no tokenisation — they carry no word boundaries).
    for char, valence in _EMOJI.items():
        count = working.count(char)
        if count:
            valences.extend([valence] * min(count, 4))

    # 3. Single tokens, with negation and intensifier context.
    tokens = _TOKEN_RE.findall(working)
    lowered = [t.lower() for t in tokens]
    for index, word in enumerate(lowered):
        base = _LEXICON.get(word)
        if base is None:
            continue
        valence = base
        if _is_shouting(tokens[index]):
            valence += _CAPS_BOOST if valence > 0 else -_CAPS_BOOST

        # Intensifiers/dampeners immediately preceding, decayed by distance.
        for offset in range(1, 3):
            prior = index - offset
            if prior < 0:
                break
            boost = _INTENSIFIERS.get(lowered[prior])
            if boost is not None:
                valence += valence * boost * (1.0 - 0.05 * (offset - 1))

        window = lowered[max(0, index - _NEGATION_WINDOW):index]
        if any(w in _NEGATIONS for w in window):
            valence *= _NEGATION_DAMPING
        valences.append(valence)

    if not valences:
        return {"compound": 0.0, "hits": 0, "positive": 0, "negative": 0}

    total = sum(valences)
    exclamations = min(raw.count("!"), _MAX_EXCLAMATIONS)
    if exclamations:
        total += math.copysign(exclamations * _EXCLAMATION_BOOST, total)

    compound = total / math.sqrt(total * total + _NORMALISATION_ALPHA)
    return {
        "compound": round(max(-1.0, min(1.0, compound)), 4),
        "hits": len(valences),
        "positive": len([v for v in valences if v > 0]),
        "negative": len([v for v in valences if v < 0]),
    }


def label(compound: float) -> str:
    """Bucket a compound score. Thresholds match VADER's conventional cutoffs."""
    if compound >= 0.05:
        return "positive"
    if compound <= -0.05:
        return "negative"
    return "neutral"


def aggregate(scores: list[float]) -> dict:
    """Mean sentiment plus the positive/negative split for a set of documents.

    The split matters more than the mean: 50 wildly bullish posts and 50
    wildly bearish ones average to zero, which is a materially different
    situation from 100 genuinely indifferent ones.
    """
    if not scores:
        return {"mean": 0.0, "positive": 0, "negative": 0, "neutral": 0, "count": 0}
    positive = len([s for s in scores if s >= 0.05])
    negative = len([s for s in scores if s <= -0.05])
    return {
        "mean": round(sum(scores) / len(scores), 4),
        "positive": positive,
        "negative": negative,
        "neutral": len(scores) - positive - negative,
        "count": len(scores),
    }
