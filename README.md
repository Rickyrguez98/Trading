# Trading Research Platform

Professional research codebase with **two independent research products** on
one shared engineering foundation. Free/public data by default; no API keys
required for the base pipelines.

> **Disclaimer.** Research and educational output only — **not financial
> advice**. Nothing here executes trades, logs in to a broker with a password,
> stores credentials, or promises returns. The one broker connection (option C,
> Interactive Brokers) is **read-only** and uses a token from your environment.
> Full text: [docs/shared/disclaimer.md](docs/shared/disclaimer.md).

## Modules

| Module | Command | What it answers | Documentation |
|---|---|---|---|
| **US Equities — Asset Selection** | `trading-us-equities` | Which U.S. common stocks look attractive over longer horizons on fundamentals, valuation, growth, quality, liquidity and bounded news sentiment? | [docs/us_equities/](docs/us_equities/README.md) |
| **US Equities — Portfolio Allocation** | `trading-portfolio` | Given that ranking and an amount of money, how much goes where — S&P 500 index core, quality names, growth satellites, a capped speculative sleeve — in three risk strategies? | [allocation.md](docs/us_equities/allocation.md) |
| **US Equities — Portfolio Monitor** | `trading-monitor` | For what you **already own**: sell, trim, hold or add — with the evidence, the money involved, and a forecast whose accuracy is measured, not assumed? | [monitoring.md](docs/us_equities/monitoring.md) |
| **Trading app** | `trading-app` | The four US-equities steps in one local web interface (dashboard, forms, background jobs, JSON API). Python ≥ 3.10. | [docs/app.md](docs/app.md) |
| **Binary Options — Signal Research** | `trading-binary-options` | For a **verified platform contract** and a 5–120 minute expiration, does intraday evidence favor `CALL`, `PUT` or `NO_TRADE`? | [docs/binary_options/](docs/binary_options/README.md) |

The two products are independent: **US Equities scores do not predict
five-minute price direction** and are never used that way.

## The US-equities investing workflow

```
 1. SELECT            2. ALLOCATE              3. BUY                 4. MONITOR
 trading-us-equities  trading-portfolio        by hand, at your       trading-monitor
 ranks ~4,800 US      turns the ranking +      broker (GBM today)     sell / trim / hold / add
 stocks               your amount into 3       using the order        on what you own, read from
                      risk strategies          ticket                 broker option A, B or C
        ▲                                                                       │
        └──────────── re-run monthly (the monitor flags a ranking > 30 days old) ┘
```

## Installation

```bash
git clone https://github.com/Rickyrguez98/Trading.git
cd Trading
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev,pdf]"      # or: make install  (pdf = statement PDFs for option A)
cp .env.example .env             # optional keys; .env is git-ignored
```

Python 3.9+. Optional extras:

| Extra | Install | Adds |
|---|---|---|
| `pdf` | `pip install -e ".[pdf]"` | Reading GBM / DriveWealth statement PDFs (broker option A) |
| `forecast` | `pip install -e ".[forecast]"` | The Chronos-2 deep-learning forecaster (**Python ≥ 3.10**, pulls in PyTorch) |
| `finbert` | `pip install -e ".[finbert]"` | FinBERT news sentiment |

Optional keys in `.env`: `FINNHUB_API_KEY` (free; turns news sentiment on —
without it the keyless fallback is often rate-limited), and
`IBKR_FLEX_TOKEN` + `IBKR_FLEX_QUERY_ID` (broker option C only).

## Step 1 — Stock selection

```bash
# quick test run on a 50-ticker sample
trading-us-equities --config configs/us_equities/default.yaml --universe sample --limit 50 --top 20

# the real thing: the full US universe
trading-us-equities --config configs/us_equities/default.yaml --top 20
```

Outputs: `data/us_equities/processed/asset_selection_results.csv` (the ranking
every later step reads), `reports/us_equities/top_candidates.md`, and
`reports/us_equities/asset_selection_summary.json` (run status, coverage,
diagnostics). Manual: [docs/us_equities/README.md](docs/us_equities/README.md).

### Re-running the full selection (do this monthly)

The monitor checks each holding's investment thesis against this ranking, and
fundamentals scores are **relative to the whole universe** — so the ranking
must come from a full run, and it goes stale. The monitor flags a ranking older
than 30 days.

1. **Optional but recommended:** put a free `FINNHUB_API_KEY` in `.env` so news
   sentiment is populated.
2. **Run it** — plan for 35–40 minutes and keep the laptop awake. Measured on
   2026-10-15: ~32 minutes to price ~4,800 stocks and ~3 for fundamentals; the
   news stage then spent ~17 minutes retrying a feed that answered "429 Too Many
   Requests" to every ticker. Rate limits are now handled automatically: calls
   are spaced, a 429 pauses and the run continues with the same stock (no stock
   is skipped for prices or fundamentals), and a blocked news feed is skipped
   after two tries, so that stage takes seconds. Details:
   [data-sources.md](docs/us_equities/data-sources.md#how-429-too-many-requests-is-handled).
   To run it in the background and keep a log:

   ```bash
   mkdir -p logs
   nohup trading-us-equities --config configs/us_equities/default.yaml --top 20 \
       > logs/selection_$(date +%Y%m%d).log 2>&1 &
   tail -f logs/selection_$(date +%Y%m%d).log      # Ctrl-C stops watching, not the run
   ```

3. **Check it succeeded** — `run_status` should be `VALID` or `PARTIAL`
   (`DIAGNOSTIC` / `INVALID` means a provider outage; re-run later):

   ```bash
   python -c "import json; s=json.load(open('reports/us_equities/asset_selection_summary.json')); print(s.get('run_status'), s.get('generated_at'))"
   ```

4. **Then** re-run allocation (step 2) if you are planning new money, and the
   monitor (step 4) for what you already hold — both pick the new ranking up
   automatically.

Useful flags: `--refresh-cache` ignores cached provider data;
`--use-cache-on-provider-failure` lets a recent cache back a failed live
fetch; `--processed-dir DIR` writes the results elsewhere (a small
`--tickers` run would otherwise overwrite the main ranking).

## Step 2 — Portfolio allocation

```bash
trading-portfolio --config configs/us_equities/portfolio.yaml --amount 25000
trading-portfolio --amount 5000 --strategy aggressive
trading-portfolio --amount 25000 --offline --instrument-price VOO=715.59 BND=73.40
```

Three strategies (conservative / balanced / aggressive), each a sleeve budget
summing to 100%: cash and bonds, an **S&P 500 index core**, **quality single
names**, small **growth satellites**, and an opt-in **hard-capped speculative**
sleeve. Weights come from the selection score, tilted toward lower volatility,
capped per name and per sector. Every position is reported in **percentage and
money**, with an order ticket. Outputs: `data/us_equities/processed/portfolio_*.csv`,
`reports/us_equities/portfolio/`. Methodology: [allocation.md](docs/us_equities/allocation.md).

## Step 3 — Buying, and choosing a broker option

Buying is **manual**: key the order ticket into your broker. For step 4 the
monitor needs to know what you hold, and you choose **how it reads that**:

| Option | Source | Effort | Best for |
|---|---|---|---|
| **A** | GBM monthly statement PDF (estado de cuenta) | none — download it from the app | a monthly check-up |
| **B** *(default, recommended)* | a holdings CSV you update after each trade | ~2 minutes per trade | exact lots and purchase dates, any day |
| **C** | Interactive Brokers Flex Web Service, **read-only** | one-time setup; needs an IBKR account | automatic daily positions |

GBM offers no API for client accounts (and Mexico's open-finance rules for
customer data are still unpublished), so automatic reading is only possible
through a broker that has one — today, IBKR. Full comparison and sources:
[broker-integration-options.md](docs/us_equities/broker-integration-options.md).

```bash
trading-monitor --list-brokers          # the three options, which one is selected, which are ready
trading-monitor --init-holdings         # option B: creates data/us_equities/holdings/my_holdings.csv
```

Pick the default in `configs/us_equities/monitoring.yaml` (`broker.option: A|B|C`)
or per run with `--broker A|B|C`. Your holdings file lives under `data/`, which
git ignores — it is never committed.

- **Option A:** `trading-monitor --broker A --holdings estado_de_cuenta_sep.pdf`.
  The extraction is written to `data/us_equities/processed/monitoring/extracted_*.csv`
  for you to audit. (Verified on synthetic statements only so far.)
- **Option B:** edit `my_holdings.csv` — one row per purchase:
  `ticker,shares,avg_cost,currency,market,purchase_date`. Markets: `US` (dollars,
  GBM Trading USA), `SIC` (US stock bought in pesos), `BMV` (Mexican listing).
- **Option C:** in IBKR Client Portal open **Performance & Reports → Flex
  Queries**. Use the gear next to *Flex Web Service Configuration* to enable the
  service and generate a token (1-year validity); then create an **Activity Flex
  Query** with *Open Positions* at *Lot* level (date format `yyyyMMdd`) and note
  its query ID (the "i" icon). Put both in `.env`, then `trading-monitor --broker C`. The token
  can only read reports; it is never stored or logged. (Tested against
  synthetic IBKR responses; not yet run against a live account.)

## Step 4 — Portfolio monitor

```bash
trading-monitor                                         # uses the configured broker option
trading-monitor --target data/us_equities/processed/portfolio_balanced.csv --cash 5000
trading-monitor --forecast chronos                      # deep-learning forecaster (extra: forecast)
trading-monitor --refresh-signals                       # also refresh news sentiment for your holdings
```

For every position: **SELL / TRIM / HOLD / ADD / REVIEW**, the shares and money,
your buy price, purchase date and holding period, P/L in pesos (SIC gains split
into the stock's dollar return and the peso move), the evidence behind the call,
and an outlook such as *"37% chance to be above +10% in ~6 months; 70% to touch
it at some point"*. Outputs: `reports/us_equities/monitoring/portfolio_monitor.md`
and `data/us_equities/processed/monitoring/monitor_orders.csv`.

How it decides (all thresholds in `configs/us_equities/monitoring.yaml`):

- **The thesis is about the business**, read from the full selection ranking:
  SELL only if fundamentals collapsed. **A loss alone, or a gain alone, never
  triggers a sale.** Index and bond funds are only ever rebalanced.
- **TRIM / ADD** only outside a ±25% band around the target weight, so small
  drifts don't churn; an ADD needs strong evidence, no earnings report within
  7 days, and actual money (`--cash` or sale proceeds).
- **Forecasts measure their own accuracy.** Each one is replayed on two years of
  history against a random walk, with corrections for testing many stocks at
  once; it may influence a decision only if it is directional and passes. On a
  real 12-stock test, Chronos-2 passed **at none of 36 stock/horizon pairs** — so
  today the outlooks describe **risk**, not direction. Details:
  [monitoring.md](docs/us_equities/monitoring.md).

## The app (optional): the whole workflow in your browser

```bash
python3.11 -m venv .venv311 && source .venv311/bin/activate   # the app needs Python >= 3.10
pip install -e ".[dev,pdf,app]"
trading-app                                                    # opens http://127.0.0.1:8765
```

A local web app over the same commands: a dashboard of all four steps, buttons
to run the full or a sample selection, an allocation form with every plan in
percent and money, the broker option picker (with a holdings-file creator and
uploader), and the monitor with its evidence and outlook — plus a JSON API at
`/api/docs`. Long runs are background jobs with a live log. It only listens on
your own machine, refuses requests from other websites, and never places
orders. Guide and architecture: [docs/app.md](docs/app.md).

## Monthly routine

```bash
trading-us-equities --config configs/us_equities/default.yaml --top 20   # 1. fresh ranking
trading-monitor --target data/us_equities/processed/portfolio_balanced.csv --cash 0   # 2. review holdings
# 3. read reports/us_equities/monitoring/portfolio_monitor.md; place any orders by hand
# 4. after trading (option B): add the new rows to data/us_equities/holdings/my_holdings.csv
```

## Binary Options — signal research

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

Signals per contract × horizon (5/10/15 and 30/60/90/120 minutes): `CALL`,
`PUT` or `NO_TRADE`, always with a machine-readable reason. Without a verified
instrument catalog every row is honestly `NO_TRADE / INSTRUMENT_AVAILABILITY_UNKNOWN`.
Outputs: `data/binary_options/processed/` and `reports/binary_options/latest/`.
Manual: [docs/binary_options/README.md](docs/binary_options/README.md).

## Architecture overview

```
src/
├── trading_common/             # generic infra: cache, rate limiting, IO, error taxonomy
├── trading_app/                # local web app: services (no framework) -> FastAPI -> pages
├── asset_selection/            # US Equities
│   ├── data_providers/         #   prices, fundamentals, news (finnhub, yahoo_rss, ...)
│   ├── fundamentals/ scoring/ sentiment/ validation/ health/
│   ├── allocation/             #   step 2: sleeves, weighting, caps, money layer
│   ├── monitoring/             #   step 4: holdings (A/B/C), valuation, signals,
│   │                           #           forecasting + validation, decisions, reports
│   └── pipelines/              #   trading-us-equities / trading-portfolio / trading-monitor
└── binary_options_selection/   # Binary Options (catalog, features, signals, evaluation)

configs/{us_equities,binary_options}/     # every threshold, documented
data/{us_equities,binary_options}/        # inputs, outputs, caches (git-ignored)
reports/{us_equities,binary_options}/     # generated reports (git-ignored)
docs/{shared,us_equities,binary_options}/ # documentation (start at docs/INDEX.md)
tests/{common,us_equities,binary_options,integration}/
```

## Data-source limitations (read before trusting any output)

- Default market data is **yfinance** (unofficial, rate-limited, no SLA);
  intraday quotes are ~15 minutes delayed and never treated as real time.
- News sentiment needs a free Finnhub key to be reliable; without it, sentiment
  is often empty and the reports say so.
- Confidence scores are **bounded heuristics, not probabilities**; forecasts are
  shown with their measured accuracy against a random walk.
- SIC values use the US price × USDMXN (close to, not exactly, the SIC quote).
  Taxes, FX spreads and slippage are not modelled.
- Details: [us_equities/data-sources.md](docs/us_equities/data-sources.md),
  [binary_options/data-sources.md](docs/binary_options/data-sources.md),
  [binary_options/limitations.md](docs/binary_options/limitations.md).

## Tests and quality

```bash
make test          # pytest (offline, deterministic)
make lint          # ruff
make quality       # lint + tests + packaging import checks
```

CI runs the same offline checks on Python 3.9 and 3.11 for every push and pull
request ([.github/workflows/quality.yml](.github/workflows/quality.yml)).

### Merging a pull request only when its checks pass

```bash
gh pr checks <number> --watch --fail-fast && gh pr merge <number> --merge
```

`--watch` waits for CI; `&&` runs the merge only if every check passed;
`--merge` keeps each commit (no squash, no history rewrite).

## Documentation

Start at **[docs/INDEX.md](docs/INDEX.md)** — per-domain architecture,
methodology (every formula and threshold), data sources, broker options,
limitations, roadmaps, and the migration guide.

## License

MIT — see [LICENSE](LICENSE).
