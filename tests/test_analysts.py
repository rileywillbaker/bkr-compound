"""Analyst agents: deterministic fallbacks and the (mocked) LLM path.

`complete_json` is always monkeypatched — no test ever performs a network
call, per the spec's mock-LLM-in-CI rule.
"""

import pytest

from sentinel.agents import analysts
from sentinel.agents.analysts import (
    all_analysts,
    deterministic_technicals_verdict,
    fundamentals_analyst,
    macro_analyst,
    news_analyst,
    options_flow_analyst,
    technicals_analyst,
)
from sentinel.agents.technicals import compute_technicals
from sentinel.agents.verdicts import EvidenceItem, LLMVerdictPayload
from sentinel.providers.llm.client import LLMError
from tests.synth import (
    make_bars,
    make_context,
    make_news,
    make_symbol_context,
    make_vix,
)

UPTREND_SNAP = compute_technicals("UP", make_bars("UP", 250, drift=0.3))
DOWNTREND_SNAP = compute_technicals("DN", make_bars("DN", 250, start=200.0, drift=-0.3))

LLM_PAYLOAD = LLMVerdictPayload(
    score=42,
    confidence=0.8,
    summary="llm interpretation",
    evidence=[EvidenceItem(source="technicals", datapoint="RSI14 strong")],
)


@pytest.fixture()
def llm_ok(monkeypatch):
    calls: list[dict] = []

    def fake(db, role, system, user, schema, endpoint=""):
        calls.append({"role": role, "endpoint": endpoint})
        return LLM_PAYLOAD

    monkeypatch.setattr(analysts, "complete_json", fake)
    return calls


@pytest.fixture()
def llm_down(monkeypatch):
    def fake(*args, **kwargs):
        raise LLMError("budget exhausted")

    monkeypatch.setattr(analysts, "complete_json", fake)


def test_deterministic_verdict_uptrend_positive():
    verdict = deterministic_technicals_verdict("UP", UPTREND_SNAP)
    assert verdict.score > 0
    assert verdict.deterministic_only
    assert -100 <= verdict.score <= 100
    assert verdict.evidence
    assert verdict.confidence == 0.5  # 250 bars >= 200


def test_deterministic_verdict_downtrend_negative():
    verdict = deterministic_technicals_verdict("DN", DOWNTREND_SNAP)
    assert verdict.score < 0


def test_technicals_analyst_llm_path(db, llm_ok):
    verdict = technicals_analyst(db, "UP", UPTREND_SNAP)
    assert verdict.score == 42
    assert verdict.confidence == 0.8
    assert not verdict.deterministic_only
    # all per-candidate analysts run on the cheap triage role (Haiku);
    # reasoning (Sonnet) is reserved for synthesis
    assert llm_ok[0]["role"] == "triage"
    assert llm_ok[0]["endpoint"] == "analyst.technicals"


def test_technicals_analyst_falls_back_when_llm_down(db, llm_down):
    verdict = technicals_analyst(db, "UP", UPTREND_SNAP)
    assert verdict.deterministic_only
    assert verdict.score > 0  # rule-based read still present


def test_technicals_analyst_use_llm_false_never_calls(db, llm_ok):
    verdict = technicals_analyst(db, "UP", UPTREND_SNAP, use_llm=False)
    assert verdict.deterministic_only
    assert llm_ok == []


def test_news_analyst_no_news_is_unavailable(db, llm_ok):
    verdict = news_analyst(db, make_symbol_context("X"))
    assert verdict.unavailable
    assert verdict.score == 0
    assert llm_ok == []


def test_news_analyst_triage_role(db, llm_ok):
    sym = make_symbol_context("X", news=make_news("X", ["Beat earnings", "Raised guidance"]))
    verdict = news_analyst(db, sym)
    assert verdict.score == 42
    assert llm_ok[0]["role"] == "triage"


def test_fundamentals_unavailable_without_data(db, llm_ok):
    sym = make_symbol_context("X", market_cap=None, pe=None, ps=None)
    verdict = fundamentals_analyst(db, sym)
    assert verdict.unavailable
    assert llm_ok == []


def test_fundamentals_llm_path(db, llm_ok):
    verdict = fundamentals_analyst(db, make_symbol_context("X"), insider_net_shares_90d=5000)
    assert verdict.score == 42
    assert llm_ok[0]["role"] == "triage"


def test_macro_unavailable_without_series(db, llm_ok):
    ctx = make_context({"X": make_symbol_context("X")})
    verdict = macro_analyst(db, ctx, ctx.symbols["X"])
    assert verdict.unavailable


def test_macro_llm_path(db, llm_ok):
    ctx = make_context({"X": make_symbol_context("X")}, macro={"VIXCLS": make_vix(18.0)})
    verdict = macro_analyst(db, ctx, ctx.symbols["X"])
    assert verdict.score == 42


def test_options_flow_stub_carries_zero_weight(db):
    verdict = options_flow_analyst(db, "X")
    assert verdict.unavailable
    assert verdict.confidence == 0.0
    assert verdict.score == 0


def test_all_analysts_returns_five_deterministic_without_llm(db, llm_ok):
    ctx = make_context({"UP": make_symbol_context("UP")})
    verdicts = all_analysts(db, ctx, "UP", UPTREND_SNAP, use_llm=False)
    assert len(verdicts) == 5
    assert [v.analyst for v in verdicts] == [
        "technicals",
        "news",
        "fundamentals",
        "macro",
        "options_flow",
    ]
    assert all(v.deterministic_only for v in verdicts)
    assert llm_ok == []
