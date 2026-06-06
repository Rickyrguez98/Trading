# Output Quality Audit

Audit of the **actual outputs** of a real full-universe run (not a unit-test
fixture). The pipeline completed successfully, but "completed" is not the same
as "correct". This document records what the generated artifacts actually say,
where they are misleading, and which commit closes each gap.

- Run mode: `full`
- Generated: `2026-06-05T21:44Z`
- Artifacts inspected:
  - `reports/asset_selection_summary.json`
  - `reports/universe_summary.json`
  - `reports/top_candidates.md`
  - `data/processed/asset_selection_results.csv`
  - `data/processed/universe_clean.csv`

## Funnel as reported

| Stage | In | Out | Reported provider failures |
|-------|----:|----:|---------------------------:|
| 1 universe | 4735 | 4735 | 0 |
| 2 prices | 4735 | 500 | **0** |
| 3 fundamentals | 500 | 150 | **0** |
| 4 sentiment | 150 | 150 | **0** |
| 5 compose+rank | 150 | 150 | 0 |

Stage 2 dropped `below_min_dollar_volume=1362` and
`insufficient_price_history=7`. Stage 3 dropped `below_min_market_cap=1`.

## Findings

### Q1 — Provider failures are reported as zero, but they are not zero

`provider_failures` is `0` at every stage. That number only counts calls that
**raised an exception**. The yfinance providers swallow an empty/invalid
response and return an *empty record* (no exception). So a ticker that yfinance
could not resolve looks identical to a ticker that is genuinely illiquid: it
gets `avg_dollar_volume = None`, fails the liquidity filter, and is silently
counted under `below_min_dollar_volume`.

The headline number "0 provider failures" is therefore **not trustworthy**. We
cannot tell, from the report, whether the APIs worked or mostly failed.

**Proof:** see Q2 — eleven well-known class-share symbols that yfinance *can*
serve (under a different spelling) were dropped at stage 2 as if illiquid.

**Closed by:** Commit 2 (classify empty vs error, count true resolution
failures) + Commit 6 (surface the counts in a validation report).

### Q2 — Class-share symbols silently fail (dot vs hyphen)

The NASDAQ Trader directory spells class shares with a dot: `BRK.B`. yfinance
expects a hyphen: `BRK-B`. We pass the canonical dotted symbol straight to
`yf.Ticker("BRK.B")`, which returns an empty frame, which we treat as "no
price history".

These eleven class shares survived universe cleaning (they are real common
stock) but **none reached the final 150**:

```
AGM.A AKO.A AKO.B BF.A BF.B BRK.A BRK.B CRD.A CRD.B HEI.A WSO.B
```

`BRK.A`/`BRK.B` (Berkshire Hathaway) and `BF.B` (Brown-Forman) are mega-/large-
caps that pass any liquidity filter trivially. Their absence is not an
economic result — it is a symbol-formatting bug. The report's
"possibly delisted"-style silence is exactly the kind of unvalidated claim we
must not make.

**Closed by:** Commit 2 — per-provider symbol normalization
(`canonical_symbol` ↔ `provider_symbol`, `BRK.B → BRK-B`), without mutating the
canonical ticker used everywhere else.

### Q3 — When-Issued instruments rank as top candidates

Two of the top-20 names are **When-Issued** lines, not ordinary shares:

| Rank | Ticker | Name |
|-----:|--------|------|
| 1 | SNDK | Sandisk Corporation - Common Stock **When-Issued** |
| 16 | CEG | Constellation Energy Corporation - Common Stock **When-Issued** |

When-Issued ("WI") lines trade conditionally before a corporate action settles;
their price/return history is short and not comparable to seasoned common stock.
SNDK shows a `+195.7%` return and `+96.9%` volatility — both artifacts of a
short WI tape — yet it ranks **#1**.

`universe.py` has name filters for ETF/warrant/unit/preferred/rights/notes but
**no When-Issued filter**, so these passed straight through.

**Closed by:** Commit 3 — explicit When-Issued / temporary-instrument filter
(default exclude) + per-reason removal stats.

### Q4 — High-volatility / weak-trend names rank near the top unlabeled

Top-ranked names include several that a human would immediately treat as
speculative:

| Ticker | return_pct | volatility_pct | rank | note |
|--------|-----------:|---------------:|-----:|------|
| SNDK | +195.7% | +96.9% | 1 | also When-Issued (Q3) |
| ONDS | +6.1% | +110.2% | 2 | extreme vol |
| CRDO | +88.4% | +97.4% | 5 | extreme vol |
| CDE | -27.6% | +73.8% | 6 | negative return, only WEAK_PRICE_TREND |

Across the 150 finalists, **23 (15%) have annualized volatility > 70%**. The
`risk_penalty` does push some of these down, but there is no hard volatility
ceiling, no explicit "this is speculative" label, and no bucket separating a
steady compounder from a 100%-vol momentum name. A reader skimming the top of
the table cannot tell them apart.

**Closed by:** Commit 4 — `HIGH_VOLATILITY` and `SPECULATIVE_MOMENTUM` flags,
configurable volatility / risk ceilings, and a `selection_bucket`
(`high_quality_core` / `growth` / `speculative` / `watchlist_only`). Volatile
names are **labeled, not deleted**.

### Q5 — Sentiment confidence is overstated

Distribution of `sentiment_confidence` across the 150 finalists:

| confidence | count |
|-----------:|------:|
| 1.00 | 147 |
| 0.83 | 2 |
| 0.67 | 1 |

Distribution of `article_count`:

| articles | count |
|---------:|------:|
| 10 | 144 |
| 9 | 3 |
| 7 | 1 |
| 6 | 1 |
| 2 | 1 |

yfinance's news endpoint returns **at most 10 articles**, so 144/150 tickers
hit exactly 10. The confidence formula
`0.5 * min(n/3, 1) + 0.5 * min(sources/3, 1)` saturates at 10 articles / 3
sources, so **98% of tickers report confidence 1.0** — maximum confidence from
a 10-headline sample with no check for duplicates, staleness, or model
suitability. That is exactly the "overstated sentiment confidence" we were told
to avoid.

We also do not persist the article-level evidence (titles, sources, URLs,
publish dates, dedupe status), so the confidence number cannot be audited.

**Closed by:** Commit 5 — confidence that accounts for article count (with a
softer ceiling), source diversity, recency, duplicate rate, and model; plus
per-ticker article metadata written to a news report.

### Q6 — Fundamentals are scored but not explained

Each row has `fundamentals_score` and pillar sub-scores, plus `missing_fields`.
But there is no per-ticker statement of *which metrics were strongest/weakest*,
whether `market_cap` was actually available, or whether the valuation pillar had
any inputs at all. The score is a number without a why.

**Closed by:** Commit 6 — per-ticker explainability fields (strongest /
weakest metrics, market-cap availability, valuation-metric availability) and a
validation report.

### Q7 — No post-run sanity check exists

Nothing inspects the finished output for the problems above. A run that ranks a
When-Issued line at #1 with falsely-perfect sentiment confidence exits `0` and
looks clean.

**Closed by:** Commit 6 — `reports/output_validation.{md,json}` that scans the
final table for excluded security types, suspected provider failures, stale
news, extreme volatility, missing market cap, overstated sentiment confidence,
and single-pillar dominance.

## What we will NOT do

- We will not delete volatile or speculative names — we label and bucket them.
- We will not fabricate or impute missing fundamentals/news to make a row look
  complete.
- We will not globally rewrite the canonical ticker; normalization stays at the
  provider boundary.
- We will not claim "0 provider failures" again without distinguishing
  *resolved-empty* from *errored* from *genuinely-illiquid*.

## Fix map

| Finding | Commit |
|---------|--------|
| Q1 silent failures reported as zero | 2, 6 |
| Q2 class-share dot/hyphen symbol bug | 2 |
| Q3 When-Issued in top candidates | 3 |
| Q4 unlabeled high-volatility ranks | 4 |
| Q5 overstated sentiment confidence | 5 |
| Q6 fundamentals not explained | 6 |
| Q7 no post-run validation | 6 |
