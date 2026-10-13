# Trading Research Platform

Professional research codebase with **two independent research products** on
one shared engineering foundation. Free/public data by default; no API keys
required for the base pipelines.

> **Disclaimer.** Research and educational output only — **not financial
> advice**. Nothing here executes trades, connects to a broker, stores
> credentials, sizes stakes, or promises returns.
> Full text: [docs/shared/disclaimer.md](docs/shared/disclaimer.md).

## Modules

| Module | Package | What it answers | Documentation |
|---|---|---|---|
| **US Equities — Asset Selection** | `asset_selection` | Which U.S. common stocks look attractive over longer horizons on fundamentals, valuation, growth, quality, liquidity, and bounded news sentiment? | [docs/us_equities/](docs/us_equities/README.md) |
| **US Equities — Portfolio Allocation** | `asset_selection.allocation` | Given that ranking and an amount of money, how much goes where — across an S&P 500 index core, quality names, growth satellites and a capped speculative sleeve — in three risk strategies? | [docs/us_equities/allocation.md](docs/us_equities/allocation.md) |
| **Binary Options — Signal Research** | `binary_options_selection` | For a **verified platform contract** and a specific 5–120 minute expiration, does current intraday evidence favor `CALL`, `PUT`, or `NO_TRADE`? | [docs/binary_options/](docs/binary_options/README.md) |

The modules are independent: **US Equities scores do not predict five-minute
price direction** and are never used that way. The only optional link is that
the equity shortlist can serve as an upstream universe filter for research.

## Architecture overview

```
src/
├── trading_common/             # generic infra only: cache, rate limiting, IO,
│                               # transport error taxonomy (no domain logic)
├── asset_selection/            # US Equities research product
└── binary_options_selection/   # Binary Options research product
    ├── catalog/                # verified platform-contract catalog layer
    ├── data_providers/         # replaceable intraday OHLCV providers
    ├── features/ scoring/ signals/   # horizon-aware evidence -> CALL/PUT/NO_TRADE
    ├── validation/ health/     # candle integrity, run status, output self-audit
    ├── evaluation/             # research-only historical replay (no look-ahead)
    └── pipelines/              # staged CLI orchestrator

configs/{us_equities,binary_options}/     # domain-separated configuration
data/{us_equities,binary_options}/        # domain-separated inputs/outputs/caches
reports/{us_equities,binary_options}/     # domain-separated reports
docs/{shared,us_equities,binary_options}/ # navigable documentation (docs/INDEX.md)
tests/{common,us_equities,binary_options,integration}/
```

## Installation

```bash
git clone https://github.com/Rickyrguez98/Trading.git
cd Trading
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"          # or: make install
cp .env.example .env             # optional; no keys required by default
```

Python 3.9+. Optional FinBERT sentiment extras for US Equities:
`pip install -e ".[finbert]"`.

## Quick start — US Equities

```bash
trading-us-equities --config configs/us_equities/default.yaml \
    --universe sample --limit 50 --top 20
# full universe:
trading-us-equities --config configs/us_equities/default.yaml --top 20
```

Outputs: `data/us_equities/processed/asset_selection_results.csv`,
`reports/us_equities/top_candidates.md`,
`reports/us_equities/asset_selection_summary.json` (+ diagnostics and output
validation). Manual: [docs/us_equities/README.md](docs/us_equities/README.md).

## Quick start — Portfolio allocation

Turns that ranking plus an amount of money into three risk-profiled
portfolios, reporting every position in **percentage and money**:

```bash
trading-portfolio --config configs/us_equities/portfolio.yaml --amount 25000
# one strategy only, or supply ETF prices when offline:
trading-portfolio --amount 5000 --strategy aggressive
trading-portfolio --amount 25000 --offline --instrument-price VOO=715.59 BND=73.40
```

Each strategy is a sleeve budget summing to 100%: cash and bonds for
stability, a broad **S&P 500 index core**, **quality single names**, small
**growth satellites**, and an opt-in **hard-capped speculative** sleeve.
Single-name weights come from the score selection already computed
(fundamentals + confidence-adjusted news − risk), tilted toward lower
volatility, then constrained by per-name and per-sector caps so no business
type dominates. Outputs land in
`data/us_equities/processed/portfolio_*.csv` and
`reports/us_equities/portfolio/`. Methodology:
[docs/us_equities/allocation.md](docs/us_equities/allocation.md).

The allocator refuses to plan on a ranking the selection run itself
distrusted, and it never invents a price: a missing price yields a dollar
target with no share count and a visible warning. It places no orders and
recommends no stake sizes.

## Quick start — Portfolio monitor

For the stocks you **already own**: SELL, TRIM, HOLD or ADD per position, with
the evidence, share counts and money, from a holdings CSV or a brokerage
statement PDF:

```bash
trading-monitor --holdings configs/us_equities/holdings_template.csv
trading-monitor --holdings estado_de_cuenta.pdf            # pip install '.[pdf]'
trading-monitor --holdings my_holdings.csv \
    --target data/us_equities/processed/portfolio_balanced.csv --cash 5000
trading-monitor --holdings my_holdings.csv --forecast chronos   # pip install '.[forecast]'
```

Values are reported in MXN by default, with SIC gains split into the stock's
dollar return and the peso move. The thesis check reads the full selection
ranking; index and bond funds are only ever rebalanced; a loss alone never
triggers a sale. Forecasts (a volatility bootstrap, or the Chronos-2 deep
learning model) are validated per stock against a random walk with
false-discovery control, and only a validated directional edge may influence
an action — on real data so far, none has. Methodology:
[docs/us_equities/monitoring.md](docs/us_equities/monitoring.md). Connecting a
broker instead of a file: [options](docs/us_equities/broker-integration-options.md).

## Quick start — Binary Options

```bash
# 1. Describe YOUR platform's actually listed contracts (schema + example):
cp configs/binary_options/instruments.example.yaml \
   data/binary_options/input/instruments.yaml   # then edit with verified data
trading-binary-options --validate-catalog-only \
    --instrument-catalog data/binary_options/input/instruments.yaml

# 2. Generate the signal matrix (every instrument x horizon):
trading-binary-options --config configs/binary_options/default.yaml \
    --instrument-catalog data/binary_options/input/instruments.yaml
```

Outputs: `data/binary_options/processed/signals_{all,short_term,extended_intraday}.csv`
and `reports/binary_options/latest/` (signal reports by horizon group, run
summary with the complete horizon matrix, provider diagnostics, output
validation, reproducibility manifest); historical runs under
`reports/binary_options/runs/<run_id>/`.

Signals per contract × horizon (short_term = 5/10/15 min;
extended_intraday = 30/60/90/120 min): `CALL` (evidence favors settlement
**above** the contract reference price), `PUT` (below), `NO_TRADE`
(availability, freshness, economics, or evidence insufficient — always with a
machine-readable reason). `recommended_action` is `BUY_CALL` / `BUY_PUT` /
`NO_TRADE` — contract purchases, never trading the underlying. Without a
verified instrument catalog every row is honestly
`NO_TRADE / INSTRUMENT_AVAILABILITY_UNKNOWN`.

## Data-source limitations (read before trusting any output)

- Default market data is **yfinance** (unofficial, rate-limited, no SLA).
  Intraday quotes are typically **~15 minutes delayed** and are never treated
  as real time: 5/10/15-minute binary signals are gated
  (`DATA_NOT_REAL_TIME`) under the default configuration.
- Confidence scores are **bounded heuristics, not probabilities**; no
  accuracy is claimed without out-of-sample evaluation against baselines.
- Contract availability and payouts come only from **your** verified catalog;
  nothing is fabricated. Details:
  [docs/us_equities/data-sources.md](docs/us_equities/data-sources.md),
  [docs/binary_options/data-sources.md](docs/binary_options/data-sources.md),
  [docs/binary_options/limitations.md](docs/binary_options/limitations.md).

## Tests and quality

```bash
make test          # pytest (offline, deterministic; 370 tests)
make lint          # ruff
make quality       # lint + tests + packaging import checks
```

CI runs the same offline checks on every push/PR
([.github/workflows/quality.yml](.github/workflows/quality.yml)).

## Documentation

Start at **[docs/INDEX.md](docs/INDEX.md)** — per-domain architecture,
methodology (every formula and threshold), data sources, instrument-catalog
schema, signal interpretation, historical evaluation, limitations, roadmaps,
and the migration guide ([docs/shared/migration.md](docs/shared/migration.md)).

## License

MIT — see [LICENSE](LICENSE).
