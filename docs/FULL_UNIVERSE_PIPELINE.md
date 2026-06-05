# Full-Universe Asset Selection Pipeline

The pipeline reduces a broad U.S. equity universe down to a ranked
shortlist using five explicit stages. Each stage has a clear input,
output, filter, and `top_k` cap, all driven by the YAML config — and
each stage publishes its in/out counts and drop reasons so the
reduction is visible in the output, not hidden in code.

> Free-API friendliness is a hard constraint. News and full
> fundamentals are the binding cost; the funnel exists so we only spend
> them on names that already look interesting on cheaper signals.

---

## Stage 1 — Universe collection

**Input:** none.
**Output:** `data/processed/universe_clean.csv` — the cleaned U.S.
common-stock universe, columns include `ticker`, `company_name`,
`exchange`, `asset_type`, `is_etf`, `is_test_issue`, `source`.

What it does:

1. Pulls `nasdaqlisted.txt` + `otherlisted.txt` from NASDAQ Trader.
   These cover NASDAQ + NYSE + NYSE American + NYSE Arca + BATS + IEX.
2. Falls back to SEC `company_tickers.json` if the primary source fails.
3. Validates ticker syntax (length, allowed characters).
4. Applies the exchange whitelist (`universe.exchanges`) if set.
5. Drops ETFs, funds, warrants, units, preferreds, rights, test issues,
   and notes unless the corresponding `universe.include_*` is `true`.

Caps:

- `pipeline.universe_max` — hard cap on the cleaned set. `null` keeps
  everything (typical for a real run).
- `run.sample_limit` / CLI `--limit N` — sample-mode cap that takes the
  first `N` rows (only when `--universe sample`).

**Typical sizes:** 12 799 raw → 4 735 cleaned commons.

---

## Stage 2 — Price / liquidity prescreen

**Input:** stage-1 universe.
**Output:** `pipeline.after_prices_top_k` (default **500**) tickers
ranked by 20-day average dollar volume.

What it does, per ticker:

1. Fetches `lookback_days` (default 90) of daily history via yfinance.
2. Computes `last_close`, `avg_daily_volume`, `avg_dollar_volume`,
   `return_pct`, and an annualized `volatility_pct`.
3. Drops names below `prices.min_avg_dollar_volume` (default $1M ADV).
4. Drops names without enough price history
   (`pipeline.min_price_history_days`, default 30 — the volatility
   computation requires this many usable daily returns).
5. Ranks survivors by `avg_dollar_volume` and keeps the top
   `after_prices_top_k`.

Why prices first? Two reasons:

- The yfinance `history()` endpoint is cheap and bulk-cachable.
- Liquidity is the cheapest knock-out: an illiquid name is not a
  candidate no matter how good its fundamentals look on paper.

Provider failures (network errors, delisted tickers) are logged,
counted in `stage["provider_failures"]`, and the ticker is treated as
missing — the pipeline never crashes on a single failed call.

---

## Stage 3 — Fundamentals prescreen

**Input:** stage-2 survivors.
**Output:** `pipeline.after_fundamentals_top_k` (default **150**)
tickers ranked by `fundamentals_score`.

What it does, per ticker:

1. Fetches fundamentals via yfinance.
2. Computes the five pillar scores cross-sectionally:
   - growth (revenue / earnings / FCF growth),
   - quality (ROE / ROA / margins),
   - valuation (P/E, fwd P/E, PEG, P/S, P/B — inverted),
   - balance sheet (D/E inverted, current ratio),
   - cash flow (FCF yield, OCF margin).
3. Drops names whose `market_cap` is known and below
   `prices.min_market_cap` (default $100M). Names where `market_cap` is
   *missing* are kept here — many small-cap fundamentals are sparse;
   they will still be penalized via `risk_penalty` later.
4. Ranks survivors by `fundamentals_score` and keeps the top
   `after_fundamentals_top_k`.

Missing-data handling is explicit: each ticker has a `missing_fields`
list and a `missing_metric_count`. Pillar weights renormalize over
present metrics, then a per-missing-field penalty is applied — so a
ticker can't sneak into the shortlist just because half its
fundamentals are blank.

---

## Stage 4 — News + sentiment

**Input:** stage-3 survivors (typically 150 tickers).
**Output:** the same set, augmented with sentiment columns.

What it does, per ticker:

1. Fetches recent headlines/summaries via yfinance news, capped by
   `sentiment.max_age_days`.
2. Scores each article on `[-1, +1]` using VADER (default) or FinBERT
   (`pip install '.[finbert]'`).
3. Aggregates per ticker:
   - average compound,
   - recency-weighted compound (exponential half-life
     `sentiment.recency_halflife_days`),
   - positive / negative / neutral ratios,
   - source diversity,
   - confidence proxy from article count + source diversity.
4. Maps to `sentiment_score ∈ [0, 100]` (50 = neutral; defaults to 50
   when the ticker has zero articles, which also fires the `NO_NEWS`
   flag).

**This is the most important stage to defer.** Running it only on the
post-fundamentals shortlist keeps the free yfinance news endpoint
usable at universe scale. If you flip `pipeline.after_fundamentals_top_k`
to `null`, you'll be calling news for every survivor of stage 2 —
expect rate-limit pain.

---

## Stage 5 — Composite + rank

**Input:** stage-4 survivors with all pillar + sentiment columns.
**Output:**
- `data/processed/asset_selection_results.csv` — the full ranked table,
- `reports/top_candidates.md` — top-`run.top_n` (default 25),
- `reports/asset_selection_summary.json` — machine-readable summary,
- `reports/universe_summary.json` — stage stats + exchange breakdown.

The composite is:

```
final_score = w_fund * F + w_growth * G + w_quality * Q + w_valuation * V
              + w_sentiment * S - w_risk * R
```

Defaults make fundamentals dominate sentiment (asserted by the test
`test_fundamentals_dominate_sentiment_at_default_weights`). The
`risk_penalty` blends liquidity, momentum, vol, and missing-data
contributions; see [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the math.

Every row carries:

- `top_driver_pillar` / `top_drag_pillar` — which pillar moved the rank,
- `reason` — single-line summary embedded in the CSV/JSON,
- `flags` — `SPECULATIVE_HYPE`, `STRONG_FUNDAMENTALS_BAD_SENTIMENT`,
  `NO_NEWS`, `LOW_SENTIMENT_CONFIDENCE`, `WEAK_PRICE_TREND`,
  `THIN_FUNDAMENTALS`, `MISSING_MARKET_CAP`,
- `missing_fields` / `missing_metric_count` — the explicit data-gap accounting.

---

## Caps cheat-sheet

| Knob                                  | Stage    | Default | Effect of `null`                            |
|---------------------------------------|----------|---------|---------------------------------------------|
| `pipeline.universe_max`               | 1        | `null`  | Keep the full cleaned universe.             |
| `run.sample_limit` (sample mode)      | 1        | `null`  | No sample cap — same as full.               |
| `pipeline.after_prices_top_k`         | 2        | `500`   | Keep every name that passes the liquidity filter. |
| `pipeline.after_fundamentals_top_k`   | 3        | `150`   | News runs on the full stage-2 set (slow!).  |
| `run.top_n`                           | 5 (report) | `25`  | All survivors rendered in the Markdown.     |

---

## Failure modes and how the pipeline handles them

| Failure                          | Pipeline behaviour |
|----------------------------------|--------------------|
| NASDAQ Trader times out          | Falls back to SEC `company_tickers.json` (no exchange info). |
| yfinance returns HTTP error      | Ticker counted as `provider_failures`; downstream sees missing values; row flagged. |
| Provider returns empty news      | Sentiment defaults to neutral 50; `NO_NEWS` flag fires; no crash. |
| Provider returns sparse fundamentals | `missing_fields` populated; `risk_penalty` increases; `THIN_FUNDAMENTALS` flag at 5+ missing. |
| All survivors lost at any stage  | Pipeline exits non-zero, but still writes `reports/universe_summary.json` so you can see where the funnel collapsed. |

The pipeline never fabricates a value to fill a gap. If you see a
score of 50.0 in the report, that is either the explicit neutral
default (sentiment with no news, missing pillar score) or a real
computed 50.0 — the `missing_fields` and flag list disambiguate.
