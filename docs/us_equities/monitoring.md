# Portfolio monitor — step 4

`trading-monitor` takes what you **already own** and recommends, for each
position, **SELL**, **TRIM**, **HOLD**, **ADD** or **REVIEW**, with the
evidence behind each call, the share count and money involved, and an outlook
on how likely and how soon the price reaches a level. Research output only:
nothing is executed and nothing here is financial advice.

```bash
# Your configured broker option (default B: data/us_equities/holdings/my_holdings.csv)
trading-monitor --init-holdings     # once, to create the holdings CSV
trading-monitor

# A specific CSV
trading-monitor --holdings my_holdings.csv

# ...or straight from a statement PDF (pip install '.[pdf]')
trading-monitor --holdings estado_de_cuenta_sep.pdf

# Rebalance against the allocation plan you bought, with new money to deploy
trading-monitor --holdings my_holdings.csv \
    --target data/us_equities/processed/portfolio_balanced.csv --cash 5000

# Deep-learning forecaster (pip install '.[forecast]', Python >= 3.10)
trading-monitor --holdings my_holdings.csv --forecast chronos
```

Outputs: `reports/us_equities/monitoring/portfolio_monitor.md` (the report),
`portfolio_monitor_summary.json`, and in
`data/us_equities/processed/monitoring/` the per-position table and
`monitor_orders.csv` — the order ticket you would key into GBM by hand.

## 1. Input: your holdings (broker option A, B or C)

The monitor reads holdings through the **broker option** you pick —
`broker.option` in `configs/us_equities/monitoring.yaml`, or
`--broker A|B|C` per run (`trading-monitor --list-brokers` shows which are
ready):

- **A** — a GBM / DriveWealth statement PDF (below);
- **B** — the holdings CSV (default; `trading-monitor --init-holdings` creates
  it under `data/`, which git ignores);
- **C** — Interactive Brokers' read-only Flex Web Service. Lot-level positions
  carry their open dates; shorts, options and non-USD/MXN positions are skipped
  and listed. Setup: [broker-integration-options.md](broker-integration-options.md).

All three produce the same lots, so everything below applies equally.

### CSV (recommended source of truth)

`configs/us_equities/holdings_template.csv`:

```csv
ticker,shares,avg_cost,currency,market,purchase_date,broker,notes
NVDA,10,3850.00,MXN,SIC,2025-03-14,GBM,
AAPL,2.5,180.20,USD,US,2025-06-02,GBM Trading USA,
WALMEX *,40,62.15,MXN,BMV,14/01/2025,GBM,
```

- One row per purchase (a *lot*); rows with the same ticker, market and
  currency are combined with a share-weighted average cost.
- **Markets.** `US` — a US listing held in dollars (GBM Trading USA). `SIC` —
  a US listing bought in pesos through the Sistema Internacional de
  Cotizaciones. `BMV` — a Mexican listing. SIC and BMV are carried in MXN.
- **Numbers** use Mexican formatting (`3,850.25`); **dates** are ISO or
  day-first (`14/03/2025`, `14-mar-2025`). Month-first US dates are rejected
  on purpose: `03/04/2025` would be ambiguous.
- Spanish or English headers both work (`Emisora`, `Títulos`,
  `Costo promedio`, `Symbol`, `Quantity`, `Cost Basis`, ...).
- A row with no positive cost is **skipped, never guessed**.

### Statement PDF

The parser reads GBM's Spanish vocabulary (Emisora, Serie, Títulos, Costo
promedio, Precio/Valor de mercado) and DriveWealth's English one, and:

- takes shares and cost **only** from the holdings table;
- reads each table's market (SIC vs BMV) from the section heading above it,
  falling back to the US universe file;
- follows a holdings table onto the next page;
- attaches **purchase dates only when that statement's buy movements explain
  the whole position**. A monthly statement lists only that month's buys;
  using a partial set would date the position wrongly, so the date is left
  blank and the report says why;
- refuses a PDF with no holdings table rather than rebuilding positions from
  movements;
- writes everything it extracted to `extracted_<name>.csv` in the template
  format, so you can audit it and re-run with `--holdings` on that file.

> **Calibration status:** tested on *synthetic* GBM- and DriveWealth-style
> statements only. A real statement may use a layout the parser does not
> anticipate; the extraction report and confidence level (high / medium /
> low) are there to make that visible. Sharing one redacted statement is the
> fastest way to verify it.

## 2. Valuation

- Positions are valued at the **raw close** (what the broker shows); returns
  and trend use the **adjusted close**, so dividends do not look like drops.
- Everything is also reported in the **reporting currency** (MXN by default).
- For an SIC position the MXN value is the US price × USDMXN, and the gain is
  split into **stock return in dollars** and **currency return**:
  `(1 + MXN return) = (1 + USD stock return) × (1 + USDMXN return)`. Each lot
  is converted at the USDMXN close of its own purchase date.

## 3. Evidence

| Factor | Source | Score |
|---|---|---|
| **Thesis** | Fundamentals score in the latest **full-universe** selection ranking | INTACT +1 · WEAKENED −1 · BROKEN −2 |
| Trend | Price vs 200-day average, 50/200 cross | UP +1 · DOWN −1 |
| Momentum | 12-1 month return | ±0.5 |
| Sentiment | Confidence-adjusted news sentiment (ignored at low confidence) | ±0.5 |
| Earnings | Last EPS surprise; beat count | +0.25 / −0.5 |
| Risk | Volatility regime spike; selection risk exclusions | −0.5 |
| Forecast | Only a **directional** model with a **validated** edge | ±0.5 |

### The thesis is about the business

- It comes from the fundamentals score of a **full-universe** ranking.
  Those scores are cross-sectional z-scores — each stock measured against the
  set scored with it — so they are only meaningful against the whole market.
  Re-scoring your 12 holdings alone would grade each one against the other
  eleven: in the first real-data test, AFL "fell" from 58.7 to 45.1 with no
  change in its business. `--refresh-signals` therefore refreshes **only news
  sentiment** (an absolute per-stock score); to refresh the thesis, re-run
  `trading-us-equities` on the full universe.
- **Price-driven exclusions** ("negative recent return", "high volatility")
  are trend and risk evidence, never a broken thesis — otherwise the monitor
  would sell because the price fell.
- **Missing data** gives UNKNOWN, never BROKEN.
- **Index and bond funds** (the allocation's index core and bond proxy, or
  anything the ranking marks as an ETF) are **STRATEGIC**: they are not
  judged on company fundamentals, are exempt from the single-stock cap, and
  are only ever rebalanced. (The first real-data run tried to sell VOO for
  "weak fundamentals" before this rule existed.)
- A ranking older than 30 days is flagged in the report and the run status.

## 4. Decision rules (first match wins)

1. **REVIEW** — no usable price or FX rate.
2. **SELL** — thesis BROKEN (fundamentals score < 40).
3. **TRIM** — weight above the target band (target × 1.25), or for a single
   stock without a target, above the concentration cap.
4. **ADD** — a STRATEGIC holding below its band (target × 0.75): rebalance.
5. **TRIM** — thesis WEAKENED *and* composite evidence negative: sell half.
6. **ADD** — thesis INTACT, composite ≥ +1.5, below the band, no earnings
   report in the next 7 days, and money available.
7. **HOLD** — everything else; a drawdown over 25% from the post-purchase
   peak adds a review flag, not a sale.

Guarantees the tests pin down:

- **A loss alone never sells, and a gain alone never sells.** Cost basis only
  shapes the outlook ("chance of getting back to break-even").
- **ADD and TRIM both wait for the band**, so small drifts do not churn.
- The concentration cap is never tighter than an equal-weight share plus the
  band (three stocks cannot all fit under 15%).
- ADDs are funded from `--cash` plus SELL/TRIM proceeds, strategic rebalances
  first, then stocks by evidence. Sale proceeds are already part of the
  portfolio and are not counted twice.
- Whole shares, commission-inclusive, minimum trade size. Taxes (ISR on
  gains), FX spreads and slippage are not modelled.

## 5. Forecasting — what it can and cannot do

**No model forecasts individual stock prices precisely.** Pretrained deep
learning forecasters have been tested on exactly this: their gains over a
random walk on stock returns are "small and sparse" (Noguer i Alonso &
Pereira Franklin, *Pretrained Time-Series Foundation Models for Financial
Return Forecasting*, arXiv:2606.27100, 2026). So the monitor does not *claim*
accuracy — it **measures** it.

### Backends

- **`bootstrap`** (default) — resamples the stock's own daily returns in
  blocks (keeping fat tails and volatility clustering), with the drift
  removed. It has **no directional view by design**: it answers "given how
  this stock moves, how wide is the range?", never "will it go up?". Its
  P(up) never enters a decision.
- **`chronos`** — Amazon's **Chronos-2** transformer (≈120M parameters),
  zero-shot. Fast: ~12 seconds for 12 stocks including validation on a
  laptop CPU. Requires `pip install '.[forecast]'` (Python ≥ 3.10).
  Chronos-Bolt is also selectable but only for its trained 10–90% range; its
  bands also stop widening past ~64 days.

### Outlook

For each horizon (21 / 63 / 126 trading days ≈ 1 / 3 / 6 months): the 10%,
median and 90% price, P(up), and for each target (+5%, +10%, +20%, and
break-even when underwater):

- **P(above at horizon)** — chance the price is above the level then;
- **P(touch)** — chance it reaches the level at any point (path models);
- **median crossing day** — the first day the forecast's median is above the
  level (none for a model with no directional view).

This is the honest form of "how much longer until it rises": a probability
and a range, not a date.

### Validation — the part that keeps it honest

For every stock, every forecast is replayed on the last ~2 years
(walk-forward: 24 windows, 21 trading days apart) and scored against a
**zero-drift random walk** with the stock's own volatility:

- score: mean pinball (quantile) loss, an approximation of CRPS;
- test: one-sided **Diebold–Mariano** with the Harvey–Leybourne–Newbold
  correction and a Newey–West variance (overlapping windows);
- **independent windows**: 24 windows at a 126-day horizon contain only ~4
  non-overlapping outcomes, too few to establish anything — such horizons can
  never be called informative (`min_effective_windows: 8`);
- **false discoveries**: monitoring 12 stocks at 3 horizons is dozens of
  tests, and a few will "win" by luck. Significance is controlled across the
  whole run with **Benjamini–Hochberg**.

A forecast influences a decision only if it is directional **and** survives
all of the above.

### What happened on real data

A 12-stock portfolio (bought 2026-06-15 at actual closing prices, mostly via
the SIC), evaluated on 2026-10-13 with Chronos-2:

- one stock/horizon pair (MTZ, 21 days) passed at p = 0.0037 before
  correction — about what luck predicts across 24 testable pairs;
- after the run-level correction (q = 0.090): **0 of 36 pairs informative**;
- one stock (ARES) produced negative price quantiles at long horizons; they
  are clipped to a minimal positive price and the model is scored down for
  them, rather than the NaN silently erasing its evaluation.

The forecaster therefore describes **risk**, not direction, for these stocks —
and the report says so on every position.

## 6. Configuration

`configs/us_equities/monitoring.yaml` — every threshold above. Highlights:

| Key | Default | Meaning |
|---|---|---|
| `run.reporting_currency` | MXN | Currency of all totals |
| `run.max_selection_age_days` | 30 | Flag a stale thesis check |
| `forecast.backend` | bootstrap | `bootstrap` or `chronos` |
| `forecast.min_effective_windows` | 8 | Independent windows needed for a claim |
| `decision.fundamentals_broken` / `_weak` | 40 / 50 | Thesis thresholds |
| `decision.rebalance_band` | 0.25 | ± band around target before acting |
| `decision.max_weight_per_name` | 0.15 | Single-stock cap without a target |
| `decision.add_threshold` | 1.5 | Composite evidence needed to add |
| `decision.min_trade_value` | 500 | Smallest order worth placing (MXN) |

## 7. Limitations

- GBM has no API, so GBM holdings come from a file (options A/B); automatic
  reading (option C) needs an Interactive Brokers account and is tested on
  synthetic responses only — see
  [broker-integration-options.md](broker-integration-options.md).
- PDF parsing is unverified against a real GBM statement.
- Mexican listings (BMV) are outside the US-equity selection model: they are
  analysed on price, trend and forecast only (thesis UNKNOWN).
- Taxes, FX spreads and slippage are not modelled; SIC values use the US price
  × USDMXN, which tracks the SIC quote closely but not exactly.
- The monitor never proposes *new* names; that is the selection and
  allocation steps' job.
