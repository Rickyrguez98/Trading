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
| Tickers       | NASDAQ Trader FTP (`nasdaqlisted`, `otherlisted`) | Includes ETFs/warrants/units — filtered out in code.                         |
| Fundamentals  | `yfinance` (Yahoo Finance, unofficial)      | Unofficial API; rate-limited; some fields are best-effort; no SLA.           |
| Prices        | `yfinance`                                  | Same as above. Use for liquidity filters, not for execution decisions.      |
| News          | `yfinance` news endpoint                    | Limited per-ticker recency and breadth. Replaceable provider.               |
| Sentiment     | VADER (lexicon-based)                       | General-purpose lexicon; **not** finance-tuned. FinBERT is plug-compatible. |

All providers implement a common interface so paid or alternative sources
(Finnhub, AlphaVantage, FMP, NewsAPI, MarketAux, SEC EDGAR direct, FinBERT)
can be swapped in without changing the pipeline. See
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

---

## Installation

```bash
# Clone, then from the repo root:
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"          # or: pip install -r requirements.txt
cp .env.example .env             # optional, only if you plan to add API keys
```

Python 3.9+ is required.

## Configuration

Default config: [`configs/default_config.yaml`](configs/default_config.yaml).
Copy and edit it to change weights, universe limits, sentiment model, etc.

Optional API keys go in `.env` — none are required for the default pipeline.

## How to run

The CLI entry point:

```bash
python -m asset_selection.pipelines.run_asset_selection \
    --config configs/default_config.yaml \
    --limit 50 \
    --top 20
```

Useful flags:

| Flag                  | Meaning                                                     |
|-----------------------|-------------------------------------------------------------|
| `--config PATH`       | Path to YAML config (defaults to `configs/default_config.yaml`). |
| `--limit N`           | Cap the number of tickers processed (good for smoke tests). |
| `--top N`             | Number of top-ranked tickers to write to the Markdown report. |
| `--refresh-cache`     | Ignore cached provider responses and re-fetch.              |
| `--no-cache`          | Disable cache entirely for this run.                        |
| `--tickers AAPL MSFT` | Run only on the given tickers (skips universe build).       |
| `--output-dir PATH`   | Override the default `reports/` directory.                  |
| `--log-level LEVEL`   | DEBUG / INFO / WARNING / ERROR.                             |

After it finishes, look at:

- `data/processed/asset_selection_results.csv` — full ranking with all metrics.
- `reports/top_candidates.md` — human-readable top-N report.
- `reports/asset_selection_summary.json` — machine-readable run summary.

## How to interpret the output

Every row carries **transparent sub-scores** so you can see *why* a ticker
ranked where it did:

- `fundamentals_score` — blended pillar score [0, 100].
- `growth_score`, `quality_score`, `valuation_score` — pillar sub-scores.
- `sentiment_score` — recency-weighted news sentiment.
- `risk_penalty` — deducted for low liquidity, missing data, high volatility.
- `final_score` — weighted composite (see config).
- `flags` — e.g. `SPECULATIVE_HYPE` (good sentiment, weak fundamentals) or
  `STRONG_FUNDAMENTALS_BAD_SENTIMENT` (worth a second look).
- `missing_fields` — explicit list of missing data points so nothing is hidden.

**A high final score is a starting point for research, not a recommendation.**

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
