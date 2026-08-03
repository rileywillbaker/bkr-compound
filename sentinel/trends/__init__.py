"""Trend Discovery Agent — find emerging themes early, from free sources only.

This package answers a different question from the rest of B-Quant. The core
pipeline asks "is THIS ticker a good trade today?". The trend agent asks
"what is starting to matter, and who benefits?" — then hands its answers to
the existing agents rather than acting on them:

    Trend Discovery (here)  finds themes and the names exposed to them
        ↓ candidates
    Stock Analysis          sentinel/agents/ + sentinel/pipeline/
        ↓ proposed order
    Risk Management         sentinel/risk/engine.py   (absolute veto)
        ↓ approved size
    Portfolio Manager       sentinel/portfolio/       (exposure, correlation)
        ↓
    Daily trend report, in dollars

Cost: every stage above is deterministic Python over free/keyless sources and
data already in the database. The only paid step is an optional single review
call per *theme* (not per ticker), capped, cached, and one-directional — see
`review.py`. Free mode never calls it and still produces the whole report.

Nothing here executes trades, and no LLM output ever becomes a number.
"""
