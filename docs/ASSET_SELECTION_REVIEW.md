# Asset Selection — Code Review

Status of the asset-selection pipeline as of the review pass. This document
is intentionally written in two passes:

- **Pass 1 — Audit findings (this commit):** what existed and what was missing.
- **Pass 2 — Follow-up commits:** each subsequent commit closes one of the gaps
  listed below and references this document.

> Scope reminder: this repository is **asset selection only**, not asset
> allocation, not backtesting, not execution. See `docs/FUTURE_ROADMAP.md`.

---

## 1. Audit methodology

- Ran the full pytest suite — baseline: **21 tests passing** in ~0.6s.
- Read every public module under `src/asset_selection/`.
- Ran the CLI end-to-end against `--tickers AAPL MSFT --no-cache`. Yahoo
  Finance returned HTTP errors during the run (a recurring yfinance issue);
  the pipeline degraded gracefully, logged warnings, and still wrote the
  three output artifacts. Confirmed the no-data path does not crash.
- Cross-checked each item on the original spec checklist against the actual
  code paths, not just module names.

## 2. What was verified working

### Sentiment
- News provider (`data_providers/news_provider.py`) attempts a real
  `yfinance` per-ticker news fetch with retries and caching.
- `sentiment/sentiment_model.py` actually scores each article via VADER
  (default) or FinBERT (optional `[finbert]` extra).
- Aggregation per ticker computes:
  - average compound sentiment,
  - **recency-weighted** compound (exponential half-life from config),
  - article count, positive / negative / neutral ratios,
  - source diversity,
  - a confidence proxy that grows with article count and source diversity.
- Sentiment feeds into the composite via the configurable
  `composite.weights.sentiment` (default 0.15).
- Existing test `test_fundamentals_dominate_sentiment_at_default_weights`
  proves sentiment does **not** dominate fundamentals at default weights.

### Historical prices
- `data_providers/prices_provider.py` pulls history via `yfinance.history()`.
- It computes `last_close`, `avg_daily_volume`, `avg_dollar_volume`,
  `return_pct` over the lookback window, and an annualized volatility proxy
  from daily returns.
- The selection pipeline does use volatility and liquidity in the
  `risk_penalty` (low ADV → +30, below market-cap floor → +25, top-quartile
  vol → ramp).

### Fundamentals
- `data_providers/fundamentals_provider.py` maps yfinance `info` to a typed
  schema and tracks which tracked fields were missing.
- `fundamentals/fundamental_scoring.py` winsorizes, z-scores, inverts
  "lower-is-better" metrics, and applies a per-missing-field penalty.
- Pillar weights and pillar combination weights are both in
  `configs/default_config.yaml`.

### Composite
- `scoring/composite_score.py` implements:
  ```
  final = w_f·F + w_g·G + w_q·Q + w_v·V + w_s·S − w_r·R
  ```
  with every weight read from `composite.weights` in the YAML.
- `flag_rows` emits `SPECULATIVE_HYPE`, `STRONG_FUNDAMENTALS_BAD_SENTIMENT`,
  `NO_NEWS`, `THIN_FUNDAMENTALS`, `MISSING_MARKET_CAP`. Each row carries a
  `reason` string with pillar values.

### Outputs
- CSV (`data/processed/asset_selection_results.csv`) — all columns, all rows.
- Markdown (`reports/top_candidates.md`) — top-N with humanized money,
  flag legend, disclaimer.
- JSON (`reports/asset_selection_summary.json`) — run metadata + top-N
  candidate records with `reason` and `warning_flags`.

## 3. Gaps identified during the audit

Each gap below is closed in a follow-up commit; this section was written
*before* the fixes, so the language reflects the original state.

### G1. Price history is collected but underused in the score
- `return_pct` is computed per ticker and written to CSV, but **no part of
  the selection logic looks at it** — only volatility and dollar volume
  contribute to `risk_penalty`. A persistently down-trending name with low
  vol gets no penalty.
- **Fix plan:** introduce a momentum component in the risk penalty that
  penalizes very negative recent returns, and add a `WEAK_PRICE_TREND` flag.
  Surface `return_pct` in the Markdown report.

### G2. Sentiment confidence / ratios are computed but not surfaced
- `positive_ratio`, `negative_ratio`, `sentiment_confidence`,
  `source_diversity` end up in the CSV (because `rank_candidates` keeps
  every column), but **not** in the JSON summary or Markdown report.
- No `LOW_SENTIMENT_CONFIDENCE` flag, even though confidence is computed.
- **Fix plan:** add the fields to the JSON summary, render confidence in the
  Markdown, add a low-confidence flag firing below a configured threshold.

### G3. `reason` only shows aggregate pillar scores
- Spec asks for "which fundamentals helped or hurt." Currently the reason
  string lists `fundamentals=70.0 | sentiment=60.0 | …`, but never names the
  pillar that drove the score up or down.
- **Fix plan:** compute `top_driver_pillar` / `top_drag_pillar` per row and
  add them to the reason string + JSON summary.

### G4. Test coverage holes against the spec
The following behaviours are not currently asserted:
- Sentiment differences with identical fundamentals actually move the rank.
- Empty / failing news provider does not crash the pipeline.
- Every top-ranked row has a non-empty `reason` and a `flags` list.
- Negative momentum is reflected in the risk penalty.
- An end-to-end smoke test that exercises the pipeline against mocked
  providers (so it doesn't depend on Yahoo being healthy).

### G5. README and methodology documentation
- Current README documents what to run, but doesn't have explicit sections
  on how sentiment / prices / fundamentals are folded into the final score
  and how to read the output.
- **Fix plan:** add a methodology section to the README and finalize this
  review doc in pass 2.

### G6. Minor schema dust
- `Fundamentals.net_income_growth` is in the schema but never populated by
  the yfinance provider. Either populate or document the gap. (Low priority,
  not in any pillar weight today.)

## 4. Out-of-scope confirmation

The following are intentionally **not** in this milestone:
- live trading / broker integration,
- portfolio construction / asset allocation,
- factor risk models,
- transaction-cost or slippage simulation,
- backtesting.

Stubs exist under `src/asset_selection/{allocation,backtesting,risk}/` and
are not imported by the pipeline.

## 5. Limitations that will remain after this review

Even after the planned fixes, the following are inherent to the data-source
choices and will not be addressed in this milestone:

- **yfinance has no SLA.** Field availability and endpoint stability shift
  without notice. The pipeline tolerates this but cannot fix it.
- **VADER is general-purpose.** It is a finance-naïve sentiment model. Use
  the optional FinBERT backend for serious research.
- **Point-in-time fundamentals are not enforced.** All fundamentals are
  pulled as-of-now. A future backtesting milestone will need historical
  filings via SEC EDGAR XBRL to avoid look-ahead bias.
- **Universe filters are heuristic.** Suffix/name rules will miss edge
  cases (foreign ADRs with unusual suffixes, dual-class shares with
  non-standard tickers). Tighten as you encounter false positives.

## 6. Recommended next milestone

**Asset allocation.** With the per-ticker `final_score`, sub-scores, and
`risk_penalty` already in a stable schema, the next milestone can add a
`BaseAllocator` interface in `src/asset_selection/allocation/` with at
least:

1. Equal-weight top-K (baseline).
2. Score-weighted with per-name and per-sector caps.
3. Risk parity using the historical covariance the prices provider already
   has access to.

See `docs/FUTURE_ROADMAP.md` for the longer plan.

---

---

## Pass 2 — what was fixed in this review cycle

Each item below references the gap ID from §3 and the commit that closed it.

### G1 — Price history is now part of the selection logic
**Closed by:** `fix: add historical price metrics to selection logic`.

- `PricesConfig` gained `weak_return_threshold` (default `-0.10`) and
  `momentum_penalty_strength` (default `20.0`).
- `compute_risk_penalty` now adds a price-trend component combining a fixed
  hit when `return_pct < weak_return_threshold` with a cross-sectional ramp
  toward the worst-observed return. Both branches together are bounded by
  `momentum_penalty_strength`.
- `flag_rows` emits the new `WEAK_PRICE_TREND` flag, and the `reason` string
  reports the recent return (e.g. `return=-12.3%`).
- JSON summary now includes `last_close` and `return_pct` per candidate.
- Test `test_negative_momentum_increases_risk_and_flags` enforces the
  monotonic relationship between negative momentum and risk_penalty.

### G2 — Sentiment confidence / ratios are now surfaced
**Closed by:** `fix: strengthen sentiment analysis integration`.

- `SentimentConfig.low_confidence_threshold` (default `0.3`) controls a new
  `LOW_SENTIMENT_CONFIDENCE` flag, which fires when there is at least one
  article but aggregated confidence is below the threshold. `NO_NEWS` keeps
  precedence at zero articles.
- JSON summary now carries `sentiment_positive_ratio`,
  `sentiment_negative_ratio`, `sentiment_confidence`, and
  `sentiment_source_diversity` per candidate.
- Markdown report shows `sentiment_confidence` and the new flag in the legend.
- Tests `test_low_sentiment_confidence_flag_fires` and
  `test_sentiment_difference_changes_final_ranking` lock the behaviour in.

### G3 — Ranking explains *which* fundamentals helped or hurt
**Closed by:** `fix: improve fundamental scoring explainability`.

- `flag_rows` computes `top_driver_pillar` and `top_drag_pillar` per row
  based on each pillar's deviation from neutral 50. They become columns on
  the ranked DataFrame and are embedded in the `reason` string.
- A pillar is only labelled if it actually moved (driver > 50, drag < 50),
  so an all-neutral row produces empty driver/drag strings rather than
  misleading ones.
- JSON summary now includes `balance_sheet_score`, `cash_flow_score`,
  `top_driver_pillar`, `top_drag_pillar`, and `missing_metric_count`.
- Test `test_top_driver_and_drag_are_emitted` enforces this.

### G4 — Test coverage now matches the spec
**Closed by:** `test: expand asset selection validation coverage`.

Added tests:
- `test_sentiment_difference_changes_final_ranking`
- `test_negative_momentum_increases_risk_and_flags`
- `test_low_sentiment_confidence_flag_fires`
- `test_top_driver_and_drag_are_emitted`
- `test_ranking_explainable_every_top_row_has_reason_and_flag_list`
- `test_pipeline_runs_end_to_end_with_mocked_providers` (integration smoke)
- `test_pipeline_does_not_crash_when_all_news_empty`

The integration tests patch the provider *factories* and exercise the real
`run_asset_selection.main` code path. They confirm the spec's
required-fields schema for every top-N candidate and prove the pipeline
degrades gracefully when news is empty for some or all tickers.

Baseline before this cycle: **21 tests passing**.
After this cycle: **28 tests passing** (`pytest -q` ≈ 0.83s).

### G5 — Documentation now explains the methodology
**Closed by:** `docs: document asset selection methodology and limitations`.

- README gained explicit sections: *How sentiment is used*, *How historical
  prices are used*, *How fundamentals are used*, *How the final score is
  calculated*, and a *Flags* table.
- The interpretation section now lists every field a row may carry,
  including the new explainability and sentiment-detail fields.
- This document was updated to its Pass 2 state above.

### G6 — Schema dust (no action this cycle)
- `Fundamentals.net_income_growth` remains in the schema and is not
  populated by the yfinance provider. It is also not referenced by any
  pillar config, so it is inert. We chose **not** to delete it: a future
  Finnhub/FMP provider can populate it without a schema change. Documented
  here as a known no-op.

## Limitations that remain

These are unchanged from §5 of the original audit and inherent to the
free-data choice:

- yfinance has no SLA — endpoint instability tolerated, not fixed.
- VADER is finance-naïve — FinBERT extras are available as a drop-in.
- Fundamentals are pulled as-of-now — point-in-time history is out of scope
  until a backtesting milestone.
- Universe filters are heuristic — will miss edge tickers.

## Recommended next milestone

Unchanged: **asset allocation**. The per-ticker `final_score`, sub-scores,
risk_penalty, and now `top_driver_pillar` / `top_drag_pillar` give an
allocator everything it needs (signal + risk hint + explainability) without
any further refactor. See `docs/FUTURE_ROADMAP.md`.
