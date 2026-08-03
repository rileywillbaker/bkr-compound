# Trend Discovery Agent

Finds emerging market themes early, works out which companies are positioned
to benefit, and hands the survivors to the existing agents for sizing and
approval. **Every data source is free and keyless.**

The core pipeline asks "is *this ticker* a good trade today?". This asks
"what is starting to matter, and who benefits?" — then refuses to act on the
answer by itself.

---

## 1. How the four agents fit together

```
Trend Discovery  sentinel/trends/      themes, strength 0-100, legitimacy,
                                       which companies are exposed, how good
                                       they are
      │ nominates candidates
      ▼
Stock Analysis   sentinel/agents/      technical snapshot; on demand, the full
                 sentinel/pipeline/    multi-agent pipeline
      │ proposed BUY + reference price
      ▼
Risk Management  sentinel/risk/        size_position() → the pure-Python risk
                 trends/allocation.py  engine. ABSOLUTE VETO, no override.
      │ approved dollar amount
      ▼
Portfolio Mgr    sentinel/portfolio/   holdings, cash, sector concentration,
                                       correlation, open-position review
      │
      ▼
                 Daily trend report, in dollars
```

The trend agent gets to **nominate**. It never gets to approve. A theme
scoring 100/100 buys nothing if the risk engine says no.

There are two hand-off points into the existing system:

* **`trend_alignment` discovery trigger** (`sentinel/data/discovery.py`) —
  constituents of a strongly-scoring theme enter the normal daily scan set and
  receive exactly the same analysis → risk gate → alert treatment as any other
  candidate. Its score is deliberately capped below the event-driven triggers:
  a theme is a reason to *look* at a name, never on its own a reason to buy it.
* **`trends/allocation.py`** — turns a ranked idea into dollars via
  `size_position()` and the real risk engine.

---

## 2. Sources — all free, and what that actually means

| Source | Access | Reliability |
|---|---|---|
| Yahoo Finance | public RSS | stable |
| CNBC (6 sections) | public RSS | stable |
| MarketWatch | Dow Jones public feeds | stable |
| Google News search | free RSS, no key | **the workhorse** |
| Seeking Alpha | public feed, often bot-checked | best effort |
| Investing.com, Nasdaq | public RSS | best effort |
| Reuters | *no free feed since 2020* | reached via Google News only |
| **Federal Register API** | documented, free, no key | stable — best free policy signal there is |
| **USAspending API** | documented, free, no key | stable — who actually receives federal money |
| DoD / DOE / NRC / White House | public agency feeds | best effort (agencies reshape their CMS) |
| Reddit | public `.json`, unauthenticated | throttled, paced hard |
| StockTwits | free v2 endpoints | throttled, progressively locked down |
| **X / Twitter** | **none** | see below |
| ETF holdings | issuer CSVs (ARK, Global X, iShares) | best effort |
| ETF flow proxy | our own bars | **always available** |

### X / Twitter is deliberately absent

There is no free tier that permits reading public posts. The remaining options
are a paid plan or scraping in breach of the terms of service. `collect_x()`
therefore returns nothing, always, and says so. Silently producing an empty X
signal would let a component score zero and quietly drag trends down; quietly
scraping would break both the ToS and the "free only" constraint. The report
states the gap instead.

### ETF flows: one reliable signal, one valuable one

Real creation/redemption data is a paid product. So:

1. **Flow proxy from our own bars — always works.** Thematic ETF tickers live
   in `config/thematic_etfs.csv` (deliberately *not* `universe_*.csv`, which is
   globbed into the tradeable universe — these must never become stock
   recommendations). Sustained above-average dollar volume plus relative
   strength versus SPY is what money entering a theme looks like from outside.
2. **Published holdings — best effort.** Where an issuer publishes free daily
   holdings, snapshots are stored per date and **diffed**. The diff is what
   answers *"what uranium companies are ETFs increasing exposure to?"*, as
   opposed to merely "what do they hold?".

An empty accumulation list means **"no free holdings data"**, never "no
accumulation". The report says which basis it used.

### Degradation contract

Free endpoints fail constantly and unremarkably — a CDN 403, a timeout, a
consent-page redirect. Every collector returns `[]` and logs; it never raises.
Collection records **which sources answered**, and scoring distinguishes:

* *covered but empty* — we looked, there was nothing → scores 0
* *not covered* — we could not look → **excluded from both numerator and
  denominator**, and reported as a coverage gap

That distinction matters: a dead feed must not read as an absence of news.

---

## 3. Sentiment: free, offline, finance-tuned

`sentinel/trends/sentiment.py` — a lexicon scorer, not a transformer.

FinBERT is free to *use* but drags in torch: multiple gigabytes to score a few
thousand short headlines a day on a machine that sleeps most of the day. The
lexicon runs in microseconds, needs no install, and is fully deterministic —
which matters because sentiment feeds a score the user sees.

It combines two open methods, implemented natively:

* **VADER's grammar handling** — negation windows, intensifiers/dampeners,
  ALL-CAPS emphasis, punctuation, and the `x / sqrt(x² + α)` compound
  normalisation.
* **Loughran-McDonald's insight** that general-purpose lexicons are wrong for
  finance. "Liability" and "tax" are not negative in a filing; "beat",
  "guidance raise" and "backlog" carry meaning generic lexicons miss.

Retail vocabulary (🚀, "bagholder", "diamond hands") is included on purpose —
it is a strong signal, but of *attention*, not quality.

ALL-CAPS emphasis explicitly ignores tickers and acronyms: `NVDA` and `FDA` are
upper-case by nature and must not act as emphasis.

---

## 4. Trend strength score (0-100)

Six weighted components, each reported individually with its evidence:

| Component | Weight | Measures |
|---|---|---|
| `market_confirmation` | 25 | did the constituent basket actually outperform |
| `news_momentum` | 22 | coverage now vs this theme's own baseline |
| `policy_support` | 18 | federal rules, awards, agency activity |
| `etf_activity` | 15 | thematic ETF strength + holdings accumulation |
| `social_attention` | 10 | mention growth and tone |
| `breadth` | 10 | how many members participate |

**Talk is discounted against action.** Market + policy = 45 outweighs news +
social = 32. A theme cannot reach a high score on coverage alone.

Policy-driven themes (defence, nuclear, infrastructure) shift 6 points from
social to policy, because that is genuinely where their information lives.

A small persistence bonus (≤5 points) rewards a score that has held up for
days. It can never manufacture a trend on its own.

### The hype guard

`assess_legitimacy()` looks for the specific signature of a hype cycle:

* loud on social while the basket is not outperforming
* heavy coverage with no market, policy or ETF confirmation
* one stock accounting for >65% of the basket's gain
* few constituents participating
* fewer than four names with data

Findings produce a **cap** on the score, and a label:

| Label | Meaning | Cap |
|---|---|---|
| `legitimate` | ≥2 independent confirmations | 100 |
| `emerging` | 1 confirmation | 85 |
| `mixed` | flags present, ≤1 confirmation | 60 |
| `hype` | ≥2 flags, no confirmation | 35 |
| `unproven` | too few components measurable | 45-55 |

The cap is **one-directional**: it can only lower a score.

---

## 5. Stock ranking — trending is necessary, never sufficient

`sentinel/trends/ranking.py`. Two guards run before any scoring.

**Quality gate** (fails CLOSED — this output has a dollar amount attached):

* price ≥ $5 — no penny stocks
* 20-day turnover ≥ $10M — must be exitable
* market cap ≥ $500M — no micro-caps
* ≥120 daily bars, known sector, fundamentals on file

**Pump-and-dump guard.** Requires the conditions *together*, not any one alone
— plenty of good stocks double, and plenty are heavily shorted:

* up ≥80% in a month, **and**
* on ≥3× normal volume, **and**
* small cap / ≥20% short interest / ≥8 social mentions (≥3 conditions total),
* **and no earnings release or 8-K to explain the move**

A real catalyst defuses it entirely. Excluded names are *reported* with the
reason, not silently dropped.

Then seven factors, each 0-1:

`financial_health` · `revenue_growth` · `momentum` · `institutional_interest` ·
`valuation` · `competitive_advantage` · `risk` (subtracted)

**Valuation is ranked within the theme's own peers**, not against absolute
thresholds. A 40× semiconductor and a 40× utility are not the same statement,
and an absolute PE cutoff would systematically exclude every growth theme this
agent exists to find.

**`competitive_advantage` is a labelled proxy.** Real moat analysis needs
segment economics and returns on incremental capital, none of which is free.
What is free: scale within the peer set, growth faster than peers, and
sustained relative strength. Those correlate with competitive position without
pretending to measure it.

Confidence falls with missing data — a high score built on three unknowns is
not the same claim as one built on complete data.

---

## 6. Dollar sizing

The report speaks in dollars because that is what is actionable. A dollar
amount is a trade parameter, so it comes only from deterministic code:

1. `size_position()` — fixed-fractional sizing from ATR and the risk profile
2. **cash cap** — never propose more than the account holds
3. `risk_evaluate()` — the real engine, all rules, absolute veto
4. portfolio context — exposure, sector, correlation

Step 2 is applied in `allocation.py` rather than as a new risk-engine rule: the
engine governs risk, not settlement, and adding a rule to a pure, heavily
tested safety module to express a cash constraint would be the wrong place. The
cap only ever makes a proposal *smaller*, so the engine's veto stays the
strictest authority.

**Fractional shares are handled honestly.** On a small account the sizer may
return zero whole shares of an expensive stock. The allocation then reports the
risk-approved *notional* and says fractional shares are required. It never
rounds up to one share to make a recommendation possible, and anything under
$10 is refused as not worth the spread.

---

## 7. Cost

| Stage | Cost |
|---|---|
| Collection (dozens of sources, thousands of documents) | **$0** |
| Theme extraction, ticker extraction, sentiment | **$0** |
| Trend scoring, all themes, every day | **$0** |
| Stock ranking, quality gate, pump guard | **$0** |
| Risk engine, sizing, portfolio review | **$0** |
| Report composition and sending | **$0** |
| Optional theme review | ≤2 calls/day (Smart), ≤4 (Research), **0 (Free)** |

The only paid step is `trends/review.py`: **one call per THEME, never per
ticker**, capped by operating mode, cached on a fingerprint of the material
evidence, and one-directional:

```
confirms   → keep the deterministic score
overstated → REDUCE it
hype       → reduce further AND force the "hype" label
```

It can never raise a score, promote a label, add a stock, change a rank, or
alter a dollar amount.

**Free mode produces the entire report** — every theme, every score, every
ranked stock, every dollar amount — with zero calls. What it loses is a second
opinion on narrative legitimacy.

---

## 8. Schedule

| Time (ET) | Job | Spends? |
|---|---|---|
| 07:45 | `job_trend_collect` — gather sources, score themes | no |
| 08:30 | (existing) pre-market discovery — consumes trend snapshots | no |
| 09:30 | (existing) open scan | maybe |
| 09:50 | `job_trend_report` — build and send the report | ≤2 calls |
| 02:00 | (existing) nightly — purges documents >60 days | no |

Collection runs **before** the pre-market job on purpose, so that job's
discovery pass sees today's snapshots through `trend_alignment`.

---

## 9. API

| Endpoint | Purpose |
|---|---|
| `GET /api/trends` | latest per-theme snapshots |
| `GET /api/trends/themes` | the taxonomy itself |
| `GET /api/trends/themes/{key}` | one theme + 30-day score history |
| `GET /api/trends/report` | most recent stored report |
| `POST /api/trends/report` | regenerate now (honours operating mode) |
| `POST /api/trends/collect` | force a collection pass (always free) |
| `GET /api/trends/documents` | the raw evidence behind the scores |
| `GET /api/trends/social` | trending tickers, mentions, sentiment, growth |
| `GET /api/trends/etf-activity` | what thematic ETFs are accumulating |

UI: **Trends** tab.

---

## 10. Extending the taxonomy

Add a `Theme` to `sentinel/trends/taxonomy.py`. Scoring, ranking, the report,
the API and the UI all iterate over `THEMES` — no other code changes.

```python
Theme(
    key="desalination",
    name="Water scarcity and desalination",
    description="...",
    keywords=("desalination", "water reuse", "brine treatment"),
    etfs=("PHO", "FIW"),
    seeds=("XYL", "ECL", "WTS"),
    gov_queries=("water infrastructure", "desalination"),
    policy_driven=True,
)
```

Seed lists are a *starting point*, not the answer: a theme's working universe
is seeds ∪ ETF holdings ∪ tickers extracted from its own news, so a company
that only shows up because it just won a contract still gets ranked.

If the theme uses new ETFs, add them to `config/thematic_etfs.csv` so their
bars are ingested for the flow proxy.

---

## 11. Safety properties

* The system still **never executes trades**.
* The risk engine is unchanged — pure Python, absolute veto, no override path.
* **No LLM output ever becomes a number.** Scores, ranks, shares, stops,
  targets and dollar amounts are all deterministic.
* The LLM sees a theme, never a ticker-level buy decision.
* Free mode is structurally incapable of spending.
* NO RECOMMENDATION is a first-class outcome — the report states plainly when
  nothing cleared the gates, which is a normal day, not a failure.
* Disclaimers on every surface and every alert.
