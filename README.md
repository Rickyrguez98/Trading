# Asset Selection Algorithm — U.S. Equities

A research prototype that ranks U.S.-listed common stocks using a transparent
combination of **fundamentals**, **growth**, **valuation**, **quality**, and
**news sentiment**, sourced exclusively from free / public APIs.

> **Disclaimer.** This project is for research and educational purposes only.
> It is **not** financial advice. It does **not** place trades, connect to any
> brokerage, or promise any returns. See [docs/DISCLAIMER.md](docs/DISCLAIMER.md).

---

## Current scope — Milestone 1: Asset Selection

The first milestone is to discriminate between assets and surface a ranked
shortlist of candidates that look attractive on both **company quality** and
**market expectations**. The pipeline:

1. Builds a U.S. common-stock universe from free, public sources.
2. Pulls fundamentals, prices, and recent news for each ticker (cached).
3. Scores sentiment per article and aggregates per ticker.
4. Computes pillar sub-scores: growth, quality, valuation, balance-sheet,
   cash-flow.
5. Combines them into a configurable composite score and ranks candidates.
6. Emits a CSV, a Markdown top-N report, and a JSON summary.

## Out of scope (for now)

- Live trading or order routing.
- Asset allocation / portfolio construction.
- Risk parity, mean-variance optimization, factor models.
- Backtesting, walk-forward analysis, transaction-cost modeling.

These are sketched in [docs/FUTURE_ROADMAP.md](docs/FUTURE_ROADMAP.md) and have
placeholder modules (`src/asset_selection/{allocation,backtesting,risk}/`) that
the current pipeline does **not** depend on.

---

## Data sources and their limitations

| Layer         | Default source                              | Limitations                                                                 |
|---------------|---------------------------------------------|-----------------------------------------------------------------------------|
| Tickers       | NASDAQ Trader FTP (`nasdaqlisted`, `otherlisted`) | Includes ETFs/warrants/units/preferreds/rights/notes and When-Issued lines — all filtered out by default in code. Class shares (e.g. `BRK.B`) are kept and mapped to the provider's notation (`BRK-B`). |
| Fundamentals  | `yfinance` (Yahoo Finance, unofficial)      | Unofficial API; rate-limited; some fields are best-effort; no SLA. Empty responses are recorded as `status="empty"`, not silently treated as illiquidity. |
| Prices        | `yfinance`                                  | Same as above. Use for liquidity filters, not for execution decisions.      |
| News          | `yfinance` news endpoint                    | Caps at ~10 articles/ticker from a narrow set of sources. Duplicates and stale items are detected and down-weighted; breadth is still limited. Replaceable provider. |
| Sentiment     | VADER (lexicon-based)                       | General-purpose lexicon; **not** finance-tuned. Confidence is capped below FinBERT's. FinBERT is plug-compatible (`pip install '.[finbert]'`). |

All providers implement a common interface so paid or alternative sources
(Finnhub, AlphaVantage, FMP, NewsAPI, MarketAux, SEC EDGAR direct, FinBERT)
can be swapped in without changing the pipeline. See
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

---

## Installation

Python **3.9+** is required. The default pipeline needs **no API keys** —
everything runs on free/public endpoints.

### 1. Clone

```bash
git clone https://github.com/Rickyrguez98/Trading.git
cd Trading
```

### 2. Create and activate a virtual environment

**macOS / Linux (bash/zsh):**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

**Windows (cmd.exe):**

```bat
py -m venv .venv
.venv\Scripts\activate.bat
```

### 3. Install

```bash
pip install --upgrade pip
pip install -e ".[dev]"          # editable install + dev/test extras
# or, minimal runtime only:        pip install -r requirements.txt
cp .env.example .env             # optional, only if you later add API keys
```

The editable install (`-e`) puts the `asset_selection` package on your path.
If you prefer not to install, prefix commands with `PYTHONPATH=src`.

> **Note on `tabulate`.** The Markdown reports use pandas' `.to_markdown()`,
> which needs `tabulate`. It is included in the dev extras; if you used the
> minimal install and see a tabulate error, run `pip install tabulate`.

## Configuration

Default config: [`configs/default_config.yaml`](configs/default_config.yaml).
Copy and edit it to change weights, universe limits, sentiment model, etc.

Optional API keys go in `.env` — none are required for the default pipeline.

## How to run

The pipeline has three modes, selected by `--universe`:

### Full universe (default)

```bash
python -m asset_selection.pipelines.run_asset_selection \
    --config configs/default_config.yaml --top 20
```

Runs the entire cleaned U.S. common-stock universe (~4 700 names across
NASDAQ + NYSE + NYSE American + NYSE Arca + BATS + IEX) through the staged
funnel — see [docs/FULL_UNIVERSE_PIPELINE.md](docs/FULL_UNIVERSE_PIPELINE.md).
This is the right mode for a real ranking. It takes a while because
yfinance is rate-limited; the staged funnel keeps news fetches bounded.

### Sample (fast testing)

```bash
python -m asset_selection.pipelines.run_asset_selection \
    --config configs/default_config.yaml --universe sample --limit 50 --top 10
```

`--limit` only applies in sample mode; the rest of the funnel still runs.
Use this for iterating on weights, debugging providers, or sanity
checks — typically completes in 1–2 minutes.

### Custom shortlist

```bash
python -m asset_selection.pipelines.run_asset_selection \
    --tickers AAPL MSFT GOOGL NVDA BRK.B BF.B --top 6
```

`--tickers` implies `--universe custom`. Stage 1 is short-circuited;
the funnel still runs prices → fundamentals → news → composite over
exactly the tickers you named. Class shares can be written in dot
notation (`BRK.B`, `BF.B`); the provider layer maps them to yfinance's
hyphen notation (`BRK-B`, `BF-B`) internally while keeping the canonical
ticker unchanged in the output.

### All flags

| Flag                                 | Meaning |
|--------------------------------------|---------|
| `--config PATH`                      | Path to YAML config (default `configs/default_config.yaml`). |
| `--universe full \| sample \| custom`| Universe mode. Defaults to the YAML's `run.mode`, which is `full`. |
| `--limit N`                          | **Sample mode only.** Cap stage-1 universe at N. Ignored in full/custom mode (warning logged). |
| `--tickers AAPL MSFT …`              | Custom mode shortcut. Always overrides `--universe`. |
| `--top N`                            | Top-N for the Markdown report. |
| `--refresh-cache`                    | Invalidate cached provider responses before running. |
| `--no-cache`                         | Disable cache entirely for this run. |
| `--output-dir PATH`                  | Override the default `reports/` directory. |
| `--log-level LEVEL`                  | DEBUG / INFO / WARNING / ERROR. |

After it finishes, look at:

- `data/processed/universe_clean.csv` — the cleaned stage-1 universe.
- `data/processed/asset_selection_results.csv` — full ranking with all metrics.
- `reports/top_candidates.md` — human-readable top-N report.
- `reports/asset_selection_summary.json` — machine-readable run summary, including stages, provider failures, and exchange breakdown.
- `reports/universe_summary.json` — universe-only report (stage stats + exchange counts), produced even when the pipeline aborts.
- `reports/output_validation.md` / `reports/output_validation.json` — a post-run self-audit of the produced candidates (see [Output validation](#output-validation) below).

## Universe and staged filtering

### What's in the universe

The default ticker universe comes from NASDAQ Trader's two symbol
directories (`nasdaqlisted.txt` + `otherlisted.txt`). Despite the
"NASDAQ" in the name, the second file covers **every major U.S.
exchange**:

| Exchange       | Coverage |
|----------------|----------|
| NASDAQ         | NASDAQ-listed common stocks |
| NYSE           | New York Stock Exchange |
| NYSE American  | (formerly AMEX) |
| NYSE Arca      | mostly ETFs |
| BATS / Cboe    | mostly ETFs |
| IEX            | small handful |

After the cleaner removes ETFs / funds / warrants / units / preferreds /
rights / notes / test issues **and When-Issued (WI) lines** (e.g. the
temporary `SNDK`/`CEG` when-issued tickers, whose short price history is not
comparable to seasoned common stock), you typically end up with **~4 700
common stocks**. Every removal is attributed to a reason and counted in
`reports/universe_summary.json`.

You can narrow this with `universe.exchanges` in the YAML — empty (the
default) keeps all of them; `[NASDAQ, NYSE]` keeps just those two.
Aliases are recognised: `AMEX` ≡ `NYSE American`, `ARCA` ≡ `NYSE Arca`,
`CBOE` ≡ `BATS`.

You can also flip per-asset-type include knobs
(`universe.include_etfs`, `include_funds`, `include_warrants`,
`include_units`, `include_preferred`, `include_rights`,
`include_test_issues`, `include_notes`, `include_when_issued`) — all
default to `false` (exclude). Legacy `exclude_*` keys are honoured for
backward compatibility.

### Why a staged funnel

Free APIs have rate limits. Pulling fundamentals **and** news **and**
prices for ~4 700 tickers in one loop would take hours and would burn
through yfinance's news endpoint long before anything useful happened.

So the pipeline reduces the universe in five explicit stages, and
**news/sentiment is only collected for the post-fundamentals
shortlist**:

```
Stage 1: cleaned universe        ~4 700
Stage 2: liquidity prescreen     ↓ top-K by avg_dollar_volume = 500
Stage 3: fundamentals prescreen  ↓ top-K by fundamentals_score = 150
Stage 4: news + sentiment        ↓ runs only on those 150
Stage 5: composite + rank        ↓ top-N (default 25) in the Markdown report
```

Each stage's `top_k` lives in `pipeline.*` in the YAML; set any to
`null` to disable that cap. The full per-stage in/out counts, dropped
reasons, provider failure counts, and timings land in
`reports/universe_summary.json`.

### How to interpret the universe report

`reports/universe_summary.json` looks like:

```json
{
  "mode": "full",
  "exchange_breakdown": { "NASDAQ": 2936, "NYSE": 1582, "NYSE American": 209, ... },
  "stages": [
    { "name": "1_universe",      "input_count": 12799, "output_count": 4735, "dropped": {} },
    { "name": "2_prices",        "input_count": 4735,  "output_count": 500,
      "dropped": {"below_min_dollar_volume": 1820, "insufficient_price_history": 12} },
    { "name": "3_fundamentals",  "input_count": 500,   "output_count": 150,
      "dropped": {"below_min_market_cap": 87}, "provider_failures": 14 },
    { "name": "4_sentiment",     "input_count": 150,   "output_count": 150,
      "provider_failures": 9 },
    { "name": "5_compose_and_rank","input_count": 150, "output_count": 150 }
  ]
}
```

`provider_failures` is how many tickers got a provider error at that
stage (cached as missing); the pipeline never crashes on a single
failed call. If you see a lot of failures, your run is correct but the
data behind those tickers will be marked missing in the output.

## Methodology

This section is the source of truth for *what the pipeline actually does*.
The code is the ultimate authority; this is the explained-to-a-human view.

### 1. How sentiment is used
- The news provider (`yfinance` by default) pulls recent headlines and
  summaries for each ticker. Articles older than `sentiment.max_age_days`
  are dropped.
- VADER (default) or FinBERT (optional, `pip install '.[finbert]'`) scores
  each remaining article on `[-1, +1]`.
- Per ticker we compute:
  - **average compound** sentiment,
  - **recency-weighted compound** via an exponential half-life
    (`sentiment.recency_halflife_days`),
  - **positive / negative / neutral ratios**,
  - **source diversity** (distinct publishers of *unique* articles),
  - **duplicate and stale accounting** — repeated wire stories (same URL or
    headline) are flagged as duplicates; articles older than
    `sentiment.stale_after_days` are flagged stale,
  - a **confidence** in `[0, 1]` that is deliberately hard to max out.
- **Confidence is a five-factor blend**, not a count that saturates at three
  articles. It rewards *unique* article volume
  (`sentiment.confidence_full_article_count`, default 25), *source diversity*
  (`sentiment.confidence_full_source_count`, default 5), *freshness*, and
  *de-duplication*, then caps the result by model quality
  (`vader_confidence_factor` 0.85 < `finbert_confidence_factor` 1.0). The
  common case of ten near-identical yfinance headlines from one source now
  scores ≈ **0.48**, not 1.0 — sentiment can no longer look certain off a thin,
  single-source feed.
- The aggregated `sentiment_score` enters the composite at
  `composite.weights.sentiment` (default **0.15** — intentionally smaller
  than fundamentals). Low confidence below
  `sentiment.low_confidence_threshold` raises the `LOW_SENTIMENT_CONFIDENCE`
  flag; zero articles raise `NO_NEWS` instead.

### 2. How historical prices are used
- The price provider (`yfinance` by default) downloads `lookback_days` of
  history per ticker.
- Per ticker we derive:
  - `last_close`,
  - `avg_daily_volume` and `avg_dollar_volume` (20-day),
  - `return_pct` over the lookback,
  - an annualized **volatility** proxy from daily returns.
- These contribute to the **risk penalty** in three ways:
  1. **Liquidity filter** — names below `prices.min_avg_dollar_volume` get
     a flat penalty plus a soft ramp.
  2. **Volatility ramp** — names above the universe's 75th-percentile vol
     get a proportional penalty.
  3. **Momentum** — names whose `return_pct < prices.weak_return_threshold`
     get a flat hit *and* a cross-sectional ramp that grows toward the
     worst-observed return. The `WEAK_PRICE_TREND` flag fires in this case.
- Names below `prices.min_market_cap` also pay a flat penalty (penny-stock
  filter).

### 3. How fundamentals are used
- The fundamentals provider returns a typed record per ticker; every numeric
  field is `Optional[float]`. Missing values become `None`, never zero.
- Five pillars are computed cross-sectionally:
  - **growth** (revenue, earnings, FCF growth)
  - **quality** (ROE, ROA, op-margin, net-margin)
  - **valuation** (P/E, fwd P/E, PEG, P/S, P/B — inverted so cheaper scores higher)
  - **balance sheet** (D/E inverted + current ratio)
  - **cash flow** (FCF yield, OCF margin)
- For each metric we winsorize (`scoring.winsor_lower_pct` /
  `winsor_upper_pct`), z-score against the universe, invert "lower is better"
  metrics, then map to `[0, 100]` via `50 + 15·z` clipped.
- Pillar weights renormalize over the *present* metrics for the ticker, so a
  single missing field doesn't crash a pillar — but a per-missing-field
  penalty (`scoring.missing_penalty_per_field`) is applied. Tickers with 5+
  missing tracked fields also fire the `THIN_FUNDAMENTALS` flag.
- Pillars combine into `fundamentals_score` via `scoring.pillars` weights.

### 4. How the final score is calculated

```
final_score =
    w_fundamentals · fundamentals_score
  + w_growth       · growth_score
  + w_quality      · quality_score
  + w_valuation    · valuation_score
  + w_sentiment    · sentiment_score
  − w_risk         · risk_penalty
```

All weights live under `composite.weights` in
[`configs/default_config.yaml`](configs/default_config.yaml). Defaults make
fundamentals + pillar weights dominate sentiment (asserted by the test
`test_fundamentals_dominate_sentiment_at_default_weights`). The final score
is clipped to `[0, 100]` and ties break by `fundamentals_score` then
`sentiment_score`.

### 5. Risk controls and selection buckets

Volatile and speculative names are **labeled, never silently dropped**, so a
steady compounder is visibly separated from a 100 %-volatility momentum name.
Thresholds live under `risk_controls` in the YAML (volatility values are
annualized fractions, `0.80` = 80 %):

- `HIGH_VOLATILITY` fires when annualized volatility exceeds
  `risk_controls.max_volatility_pct` (default 0.80).
- `SPECULATIVE_MOMENTUM` fires when a big run-up rides a very noisy tape —
  return ≥ `speculative_return_pct` **and** volatility ≥
  `speculative_volatility_pct`.

Every ranked row also gets a `selection_bucket`, assigned by priority:

| Bucket | Meaning |
|--------|---------|
| `speculative_candidate`        | High volatility, speculative momentum/hype, or a high risk penalty. Labeled, not removed. |
| `watchlist_only`               | Weak trend, thin/poor fundamentals, or missing market cap; needs more review before sizing. |
| `high_quality_core_candidate`  | Strong fundamentals (≥ `core_min_fundamentals`), contained volatility, low risk penalty, non-negative trend. |
| `growth_candidate`             | Decent fundamentals with elevated — but not extreme — volatility. |

### 6. Flags

| Flag                                 | Meaning |
|--------------------------------------|---------|
| `SPECULATIVE_HYPE`                   | Strong sentiment but weak fundamentals — treat with caution. |
| `STRONG_FUNDAMENTALS_BAD_SENTIMENT`  | Quality business with bad recent news — worth a closer look, not an auto-reject. |
| `NO_NEWS`                            | No recent articles available; sentiment defaulted to neutral. |
| `LOW_SENTIMENT_CONFIDENCE`           | Some news, but few unique articles, low diversity, stale, or duplicated. |
| `WEAK_PRICE_TREND`                   | Recent return below the configured weak-return threshold. |
| `THIN_FUNDAMENTALS`                  | 5+ tracked fundamental fields missing. |
| `MISSING_MARKET_CAP`                 | Couldn't read market cap; size/liquidity filters degraded. |
| `HIGH_VOLATILITY`                    | Annualized volatility above `risk_controls.max_volatility_pct`. |
| `SPECULATIVE_MOMENTUM`               | Large run-up on a very noisy tape; reward may be chasing risk. |

## How to interpret the output

Every row carries **transparent sub-scores** plus the *driver* and *drag*
pillar so you can see *why* a ticker ranked where it did:

- `fundamentals_score` — blended pillar score [0, 100].
- `growth_score`, `quality_score`, `valuation_score`, `balance_sheet_score`,
  `cash_flow_score` — pillar sub-scores.
- `strongest_metric` / `weakest_metric` (+ their `_score`) — the single best
  and worst individual metric behind the fundamentals score, so you can see
  *what* drove it rather than trusting an opaque number.
- `market_cap_available`, `valuation_metrics_available` — fundamentals
  data-coverage flags (how many of the 5 valuation ratios were present).
- `sentiment_score`, `article_count`, `unique_article_count`,
  `duplicate_count`, `stale_count`, `fresh_ratio`, `unique_ratio`,
  `sentiment_confidence`, `positive_ratio`, `negative_ratio`,
  `source_diversity`, `sentiment_model` — sentiment block.
- `last_close`, `return_pct`, `volatility_pct`, `avg_dollar_volume`,
  `market_cap` — price / liquidity block.
- `risk_penalty` — combined liquidity / momentum / vol / missing-data hit.
- `selection_bucket` — core / growth / speculative / watchlist label
  (see [Risk controls and selection buckets](#5-risk-controls-and-selection-buckets)).
- `top_driver_pillar` / `top_drag_pillar` — the pillar that most lifted /
  hurt the score above / below neutral 50.
- `final_score` — weighted composite (see config).
- `reason` — single-line, human-readable summary of the row.
- `flags` — list of warning flags (see the table above).
- `missing_fields` / `missing_metric_count` — explicit accounting of what
  data was unavailable.

**A high final score is a starting point for research, not a recommendation.**

## Output validation

After ranking, the pipeline re-audits its own output the way a skeptical
reviewer would and writes `reports/output_validation.md` (plus a `.json`
twin). **Nothing here drops rows — it only reports.** Each check is `ok` or
`warn`; a warning means "look closer," not "this was removed." The checks:

| Check | What it catches |
|-------|-----------------|
| `excluded_security_types_in_results` | An ETF / warrant / unit / preferred / rights / note / when-issued line that leaked past universe cleaning. |
| `provider_failures`                  | How many provider calls errored or returned empty, by reason — so a run isn't trusted just because it finished. |
| `stale_news`                         | Candidates whose sentiment leans on aging coverage. |
| `extreme_volatility`                 | Names above the volatility ceiling (labeled, not removed). |
| `missing_market_cap`                 | Names with degraded size/liquidity filtering. |
| `overestimated_sentiment_confidence` | Confidence ≥ 0.80 on a thin or single-source feed. |
| `single_pillar_dominance`            | A fundamentals score carried by one pillar while the others are weak. |

This is the honesty layer: it makes data gaps and quality caveats explicit
instead of letting a green "pipeline completed" message imply the results are
clean.

## Relevance to future portfolio rebalancing

This milestone deliberately stops at **selection** — producing a transparent,
explainable ranked shortlist — and does **not** size positions. But the output
is designed to feed a future allocation / rebalancing milestone:

- **`selection_bucket`** gives an allocator ready-made risk tiers. A future
  rebalancer can cap or scale exposure per bucket (e.g. core gets full weight,
  speculative is capped, watchlist is excluded until reviewed) instead of
  blindly weighting by score.
- **`risk_penalty`, `volatility_pct`, `return_pct`** are the raw inputs a
  risk-aware weighting scheme (risk parity, vol targeting) would consume.
- **`final_score` and the pillar sub-scores** can drive a score-tilted weight,
  while **`sentiment_confidence` and the data-coverage fields** let an
  allocator *discount* names whose signal is thin or poorly disclosed.
- **The validation report** gives a rebalancer a pre-trade gate: refuse to size
  a name that tripped `excluded_security_types_in_results` or
  `missing_market_cap`.

The placeholder packages `src/asset_selection/{allocation,backtesting,risk}/`
mark where that work will live. They are **not** wired into the current
pipeline — see [docs/FUTURE_ROADMAP.md](docs/FUTURE_ROADMAP.md).

---

## Project layout

```
.
├── configs/default_config.yaml
├── data/{raw,processed,cache}/
├── docs/
├── notebooks/
├── reports/
├── src/asset_selection/
│   ├── config.py
│   ├── logging_config.py
│   ├── universe.py
│   ├── data_providers/
│   ├── sentiment/
│   ├── fundamentals/
│   ├── scoring/
│   ├── validation/          # post-run output self-audit
│   ├── pipelines/run_asset_selection.py
│   ├── utils/
│   ├── allocation/        # future milestone — placeholder
│   ├── backtesting/       # future milestone — placeholder
│   └── risk/              # future milestone — placeholder
└── tests/
```

## Development

```bash
pytest                       # run the test suite
ruff check src tests          # lint
```

## License

MIT. See [LICENSE](LICENSE).
