# Binary Options — Signal Research Module

> Research output only — not financial advice. Binary options are high-risk,
> all-or-nothing instruments. See [limitations.md](limitations.md) and
> [../shared/disclaimer.md](../shared/disclaimer.md).

Contract-aware intraday directional research: for every **verified platform
contract × configured expiration**, the pipeline answers `CALL`, `PUT`, or
`NO_TRADE` — with `recommended_action` `BUY_CALL` / `BUY_PUT` / `NO_TRADE`
(the purchase of an upward or downward contract; never trading the
underlying, and never `SELL`, which would require platform-specific
close-out modeling).

## Two concepts, kept strictly apart

1. **Underlying market instrument** — the stock/ETF/forex pair/index/crypto
   whose candles are analyzed.
2. **Binary-options contract** — the platform product with its own
   availability, expirations, payout, and reference quote.

Market data existing for a symbol **never** implies a tradable contract.
Signals become actionable only when a **verified instrument catalog** backs
them; otherwise every row is `NO_TRADE / INSTRUMENT_AVAILABILITY_UNKNOWN`.
Generic watchlists exist only behind the explicit `--research-mode` flag.

## Horizons

| Group | Expirations | Notes |
|---|---|---|
| `short_term` | 5, 10, 15 min | strictest freshness; sentiment weight 0; default yfinance feed is honestly rejected here (`DATA_NOT_REAL_TIME`) |
| `extended_intraday` | 30, 60, 90, 120 min | still intraday analysis — **not** long-term investing |

Every instrument × horizon is scored **independently** (horizon-scaled
feature windows, per-group weights); one generic direction is never reused
across expirations, and unavailable expirations stay visible in the matrix as
`NO_TRADE / EXPIRATION_NOT_AVAILABLE`.

## Quick start

```bash
# describe YOUR platform's verified contracts (never invented for you):
cp configs/binary_options/instruments.example.yaml \
   data/binary_options/input/instruments.yaml     # edit with real data
trading-binary-options --validate-catalog-only \
    --instrument-catalog data/binary_options/input/instruments.yaml

# run the full matrix:
trading-binary-options --config configs/binary_options/default.yaml \
    --instrument-catalog data/binary_options/input/instruments.yaml

# explicit research mode on generic symbols (no contract claims):
trading-binary-options --research-mode --universe watchlist
```

All CLI flags: `--config --platform --instrument-catalog --catalog-provider
--market-data-provider --asset-types --horizons --interval --top
--exclude-otc --allow-otc --research-mode --refresh-cache --no-cache
--output-dir --health-check-only --validate-catalog-only --log-level`
(plus `--tickers/--watchlist/--universe/--asset-selection-input`,
`--allow-extended-hours`, `--allow/--no-partial-results`).

## Outputs

- `data/binary_options/processed/signals_{all,short_term,extended_intraday}.csv`
- `reports/binary_options/latest/` — `signals_*.md` (status banner, complete
  horizon matrix, best CALL/PUT tables, NO_TRADE breakdown, unavailable
  expirations, stale instruments, limitations, disclaimer),
  `run_summary.json`, `provider_diagnostics.{md,json}`,
  `output_validation.{md,json}`, `run_manifest.json`
- `reports/binary_options/runs/<run_id>/` — immutable per-run history

## Reading the scores

`confidence_score` is a bounded heuristic (margin × agreement × data quality
× coverage × volatility regime). **It is not a probability** and no expected
value is ever derived from it. Payout ratio = net profit per unit staked;
`break_even_win_probability = 1 / (1 + payout_ratio)` is contract context
only. Full definitions: [signal-interpretation.md](signal-interpretation.md),
formulas: [methodology.md](methodology.md), historical evaluation:
[evaluation.md](evaluation.md).
