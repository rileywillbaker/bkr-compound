"""Swing-trading book — a separate, self-contained pipeline for 2–10 day
setups, running ALONGSIDE the long-term ("core") system without modifying it.

Everything safety-critical is reused unchanged: the deterministic risk engine
(sentinel.risk.engine.evaluate — same absolute veto, no override path), the
deterministic position sizer, the technical indicators, the analysts, and the
signal synthesizer. What is new here is only: swing-tuned strategies, a
swing-suitability screener over the S&P 500 universe, an orchestration runner
that tags its signals book="swing", and a swing alert channel with its own
daily cap. No LLM output ever becomes a trade number (spec §11).
"""
