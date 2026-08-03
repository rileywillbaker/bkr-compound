# Cost model

B-Quant treats API tokens as a scarce resource. Deterministic computation is
unlimited and free; every LLM call has to earn its place. This document is the
map of where money can be spent and what stops it.

The guiding rule: **Claude is the last step in the pipeline, never the first.**
If deterministic Python can produce an answer, no LLM is called.

---

## 1. The funnel

```
Universe  ~600+ tickers          S&P 500 ∪ Nasdaq-100 ∪ liquid large caps
    │                            ∪ your watchlist ∪ your holdings
    ▼  deterministic technical filters          (pure arithmetic over stored bars)
    ▼  liquidity filters                        (price ≥ $5, ADV ≥ $5M)
    ▼  fundamental / quality filters            (cap ≥ $2B, sector known, 200+ bars)
    ▼  analyst fact tables                      (deterministic, no LLM)
    ▼  strategy fit + position sizing           (rules + arithmetic)
    ▼  RISK ENGINE pre-filter                   (the real engine, run before any spend)
    ▼  event gate + hard cap                    (has something material happened?)
    ▼  LLM REVIEW              ≤ 3/day          ← the only place money is spent
    ▼  synthesis                                (numbers deterministic, prose from the review)
    ▼  RISK GATE                                FINAL AUTHORITY, no override
```

Every stage above the LLM row costs CPU and nothing else. That is why the
universe could grow past 600 names without the bill moving.

The risk engine appears twice on purpose:

* **`risk_prefilter`** is an *economic* gate. It guarantees the model is never
  shown a candidate the engine would reject, so tokens are never spent
  interpreting a trade that cannot happen.
* **`risk_gate`** is the *authoritative* one. It runs on every signal after all
  interpretation, exactly as it always did. The engine is a pure function, so
  evaluating twice costs nothing and cannot disagree with itself.

---

## 2. What changed, and what it saved

The old pipeline spent **seven calls per screened candidate**: five analysts,
a strategy tie-break, and a synthesis narrative — on every scan, three times a
trading day, plus twice more for the swing book.

It now spends **one cached call per finalist**.

| | Before | After (Smart mode) |
|---|---|---|
| Calls per screened candidate | ~7 | 0 |
| Calls per *finalist* | ~7 | 1 |
| Finalists per trading day | unbounded by design | ≤ 3 core + ≤ 2 swing |
| Calls on an unchanged setup | full price, every scan | 0 (cache hit) |
| Calls on a risk-vetoed candidate | full price | 0 (never reaches the model) |
| Calls in Free mode | n/a | 0, structurally |

The single review call sees strictly *more* than the old fan-out did — the
technical snapshot, screen scores, fundamentals, headlines, macro, the chosen
strategy, the computed levels and the risk verdict all arrive in one prompt —
so consolidating them improved the judgement as well as the price.

---

### The trend funnel

The Trend Discovery Agent (`sentinel/trends/`, see `docs/TREND_DISCOVERY.md`)
adds a second funnel with the same shape and the same rule.

```
Free sources        news RSS, Federal Register, USAspending, Reddit,
    │               StockTwits, ETF holdings — all keyless
    ▼  theme + ticker extraction        (regex over the whole corpus)
    ▼  lexicon sentiment                (offline, no dependency)
    ▼  trend scoring, all 14 themes     (arithmetic over stored rows)
    ▼  hype guard                       (caps the score, one-directional)
    ▼  quality gate + pump filter       (fails closed)
    ▼  seven-factor stock ranking       (deterministic)
    ▼  RISK ENGINE                      (sizing → cash cap → veto)
    ▼  LLM THEME REVIEW  ≤2/day         ← the only spend, per THEME not ticker
    ▼  report, in dollars
```

Everything above the review row is $0. The review is capped at 2 calls/day in
Smart and 4 in Research, **0 in Free**, cached on a fingerprint of the material
evidence, and one-directional: `confirms` keeps the score, `overstated` reduces
it, `hype` reduces it further and forces the hype label. Free mode produces the
complete report — every theme, score, ranked stock and dollar amount — with no
calls at all.

The rule this funnel exists to respect: collection and extraction run over
*thousands* of documents a day, so they are exactly the stages that must never
call a model.

---

## 3. Operating modes

Set in **Settings → Operating mode**. The mode governs *automatic* spend only;
it never touches the risk engine, position sizing, or any deterministic output.

| Mode | Scheduled scans | You ask directly | Target cost |
|---|---|---|---|
| **Free** | no AI at all | no AI at all | **$0/month** |
| **Smart** (default) | one review call per finalist, ≤ 3 | full multi-agent analysis | 1–5 analyses/day |
| **Research** | full multi-agent analysis on finalists | full multi-agent analysis | several × Smart |

Free mode is not a degraded mode. Screening, technicals, discovery, position
sizing, the risk engine, portfolio management, alerts and the daily briefs all
run identically. What you lose is the written narrative and the advisory
stance — the recommendation itself, and every number in it, is unchanged.

---

## 4. Event-based triggering

A finalist still needs a *reason* to be worth interpreting. At least one of:

| Trigger | Source |
|---|---|
| `earnings` | EPS surprise, or a streak of beats |
| `filing` | fresh 8-K |
| `news` | material-event keyword or a news-volume spike |
| `breakout` | pushing into 52-week-high territory |
| `pullback` | meaningful drawdown, or a dip inside an intact uptrend |
| `volume` | unusual volume vs its own 20-day average |
| `insider` | cluster of insider buying |
| `momentum` | relative strength / sector leadership |
| `position` | it's an open position with a proposed change |
| `conviction` | a genuinely high-confidence deterministic setup |
| `requested` | you asked — always wins |

Triggers are ranked by priority and truncated to the mode's cap, so a chaotic
market day cannot blow the budget: the cap binds before the trigger list does.

---

## 5. Caching

`cache_entries` (see `sentinel/data/cache.py`) holds two kinds of savings.

**Provider data that barely changes** — refetching these daily for hundreds of
tickers burns free-tier rate limit for nothing:

| Data | TTL |
|---|---|
| Company profile (sector, exchange) | 30 days |
| Fundamentals (PE, PS, beta, 52-week range) | 7 days |
| Market cap | 7 days |
| Analyst ratings | 7 days |
| Earnings calendar | 20 hours |
| Short interest | 3 days |
| SEC filing summaries | 90 days (a filed document never changes) |

**Repeated AI analysis of an unchanged situation.** Every review is keyed by a
*fingerprint* of the facts that drove it — bucketed indicators, the strategy,
the action, the regime, the headline text, the earnings date, the risk verdict.
Noise (a cent of drift, a third decimal of an EMA) is deliberately excluded so
a quiet day doesn't invalidate the cache; genuinely new headlines always force
a fresh review.

The nightly job purges expired rows. Every cache row is safe to delete — the
system just does more work afterwards.

---

## 6. Ingestion tiers

Bars are cheap and every technical trigger needs them, so they cover the whole
universe. The expensive per-symbol calls are pointed at a shortlist instead.

| Tier | Scope | What |
|---|---|---|
| 1 | full universe | daily bars, macro, earnings calendar |
| 2 | **focus set** (~60) | news, filings, insider transactions, fundamentals |
| 3 | discovery candidates | bars top-up, fundamentals, quotes, short interest |

The focus set is `technical_focus_set()` — the universe ranked by relative
strength and trend position, purely from stored bars — unioned with yesterday's
candidates, your watchlist, and everything you hold. Set
`full_universe_deep_ingest` in Settings to restore the old sweep-everything
behaviour (roughly 10× the provider calls for a marginal gain).

---

## 7. Backstops

Three independent caps, all enforced in `sentinel/providers/llm/client.py`:

1. **Dollar cap** — `policy.daily_cost_budget_usd` in `config/models.yaml`
   (default $0.25/UTC day). Smart mode's steady state is well under $0.10, so
   this is a runaway-loop backstop, not an operating target.
2. **Call-count cap** — `policy.max_llm_calls_per_day` (default 25). Catches
   the case the dollar meter can't: a model LiteLLM cannot price reports $0.00
   forever.
3. **Token cap** — `llm_daily_token_budget` in settings.

Hitting any of them raises `BudgetExceeded`, which is an `LLMError`, so every
call site degrades automatically to its deterministic fallback. Signals keep
being produced and are flagged `deterministic_only`. **There is no override.**

`GET /api/system/budget` reports live spend against all three, plus cache
health — so "near-zero cost" is verifiable rather than asserted.

---

## 8. Safety properties preserved

None of the above weakens the architecture:

* The system still **never executes trades**. No live order endpoints exist.
* The risk engine is still pure Python with an **absolute veto and no override
  path**, and still runs on every signal.
* **No LLM output ever becomes a trade parameter.** Shares, entries, stops and
  targets come only from `size_position` and market data.
* The review returns a **categorical stance** from a fixed set, never a number.
  Code owns the mapping, and it is one-directional: `confirm` keeps the
  deterministic confidence, `caution` reduces it, `reject` downgrades the
  action to NO_TRADE. A review can only make the system more conservative.
* **NO TRADE remains a first-class outcome.** Nothing here optimises for
  producing more signals.
* Models are still resolved by role through `config/models.yaml`, never
  hardcoded.
