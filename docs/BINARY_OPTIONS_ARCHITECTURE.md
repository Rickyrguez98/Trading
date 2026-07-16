# Binary Options Selection — Architecture

> Research output only. Not financial advice. This module produces research
> signals; it does not execute trades, connect to a broker, size stakes, or
> promise returns.

This document defines the architecture of the **binary options selection**
module (`src/binary_options_selection/`), the second research module in this
repository. It selects candidate underlyings and a directional research signal
(`CALL` / `PUT` / `NO_TRADE`) per **asset × expiration horizon**, for
configurable intraday horizons between **5 and 120 minutes**.

It is deliberately independent of the equity **asset-selection** module
(`src/asset_selection/`): fundamentals-driven multi-week ranking logic is not
predictive of 5-minute price direction, so the selection logic is built from
intraday data. What *is* reused is the repository's engineering discipline.

## 1. Repository audit — what is reused and why

The audit of the existing module identified these principles, all of which
this module adopts:

| Principle | Where it lives today | How this module reuses it |
| --- | --- | --- |
| Explainability | pillar sub-scores, drivers, flags | per-family signed scores, `primary_drivers`, `primary_conflicts` |
| Replaceable providers | `DataProvider` ABC + registry | `IntradayCandlesProvider` ABC + registry |
| Configurable scoring | YAML weights + typed dataclasses | horizon-group feature-family weights in `binary_options_config.yaml` |
| Honest missing data | `missing_fields`, no silent imputation | missing candles are counted and flagged, never filled/interpolated |
| Provider health checks | `health/provider_health.py` | intraday benchmark probe + `--health-check-only` |
| Caching & rate limiting | `utils/cache.py`, `utils/rate_limiter.py` | shared via `trading_common`, with **interval-aware TTLs** |
| Error taxonomy | `data_providers/errors.py` | generic transport taxonomy shared; intraday-specific codes added |
| Structured diagnostics | `provider_diagnostics.*` | `binary_options_provider_diagnostics.{md,json}` |
| Warning flags | `flag_rows`, run-status banner | per-row `warning_flags`, `no_trade_reason`, run-status banner |
| CSV + Markdown + JSON outputs | `reports/`, `data/processed/` | same triple, `binary_options_*` filenames |
| Deterministic, testable stages | `StageStats` per stage | same stage-record pattern, injectable `now` for determinism |

What is deliberately **not** reused: the fundamentals/valuation scoring model,
the news-first sentiment weighting, and multi-day cache TTL assumptions. A
strong long-horizon fundamental score is used — optionally — only as an
upstream universe/quality filter, never as a direct predictor of intraday
direction.

Baseline before this module was added: the full test suite passed
(169 tests), and the equity pipeline's CLI and outputs are unchanged.

## 2. Shared foundation — `trading_common`

Genuinely generic, domain-free components are extracted into
`src/trading_common/` so both modules use one implementation:

```
src/trading_common/
├── cache.py          # content-addressed on-disk JSON cache (moved)
├── rate_limiter.py   # minimum-interval request pacing (moved)
├── io.py             # ensure_dir / write_json / write_csv helpers (moved)
└── errors.py         # generic transport error taxonomy + classifiers (moved)
```

Backward compatibility: the original `asset_selection.utils.{cache,rate_limiter,io}`
and `asset_selection.data_providers.errors` modules remain as re-export shims,
so every existing import path keeps working. Domain-specific error codes
(`NO_PRICE_DATA`, price-coverage refinements, …) stay in
`asset_selection.data_providers.errors`. The existing test suite proves the
extraction is behavior-neutral.

## 3. Package layout

```
src/binary_options_selection/
├── __init__.py
├── config.py                 # typed config tree + strict validation + loader
├── universe.py               # custom / watchlist / asset-selection universes
├── market_session.py         # exchange-session classification + horizon alignment
├── data_providers/
│   ├── base.py               # Candle, IntradaySeries, IntradayCandlesProvider ABC
│   ├── errors.py             # intraday error taxonomy (extends trading_common)
│   ├── yfinance_intraday.py  # default free intraday provider
│   └── __init__.py           # provider registry / factory
├── features/
│   ├── indicators.py         # pure, documented indicator functions
│   └── engine.py             # feature families -> signed family scores
├── scoring/
│   └── directional.py        # horizon-aware CALL/PUT scoring + confidence
├── signals/
│   ├── decision.py           # CALL / PUT / NO_TRADE rules, validity, strength
│   ├── payout.py             # break-even math (contextual only)
│   └── labels.py             # future-outcome definitions (isolated; not used live)
├── validation/
│   ├── candles.py            # candle-series integrity + data-quality scoring
│   ├── run_status.py         # VALID / PARTIAL / DEGRADED / INVALID
│   └── output_validation.py  # post-output self-audit
├── health/
│   └── provider_health.py    # intraday provider probe
├── pipelines/
│   └── run_binary_options_selection.py   # staged CLI orchestrator
└── utils/
    └── timeutils.py          # UTC-first parsing, interval math, candle boundaries
```

## 4. Staged pipeline

```
Stage 1  Universe            custom | watchlist | asset-selection shortlist
Stage 2  Provider/session    market-state detection + optional health probe
Stage 3  Intraday candles    fetch + integrity validation (per symbol + benchmark)
Stage 4  Quality screening   liquidity, history, staleness, data-quality score
Stage 5  Features            interval-aware indicator families per symbol
Stage 6  Scoring             per (symbol × horizon): CALL/PUT/directional/confidence
Stage 7  Decision            validity gates, contradiction rules -> signal
Stage 8  Ranking             deterministic ordering of VALID signals only
Stage 9  Outputs             CSV + Markdown + JSON + provider diagnostics
Stage 10 Output validation   post-run self-audit (binary_options_output_validation)
```

Every stage records input/output counts, duration, per-reason drops, provider
failures, and warnings — the `StageStats` pattern from the equity pipeline.
No symbol is discarded silently: every exclusion carries a machine-readable
reason.

## 5. Horizons

Configurable horizons: **5–120 minutes** (default set: 5, 10, 15, 30, 60, 90,
120). Groups:

* `short` — 5–30 minutes inclusive;
* `extended_intraday` — >30 up to 120 minutes. This is still **intraday
  analysis**, not long-term investing.

Each requested (asset, horizon) pair gets its **own** feature windows, weights,
score, and signal. One generic signal is never reused across expirations:
feature lookback windows scale with the horizon, and feature-family weights are
configured per horizon group.

## 6. Directional scoring (summary — full formulas in METHODOLOGY doc)

Each feature family produces a signed score `s_f ∈ [-1, +1]` (+ = bullish
evidence). With per-horizon-group weights `w_f` (renormalized over the families
actually available):

```
call_raw          = Σ_f w_f · max(s_f, 0)
put_raw           = Σ_f w_f · max(-s_f, 0)
call_score        = 100 · call_raw                  ∈ [0, 100]
put_score         = 100 · put_raw                   ∈ [0, 100]
directional_score = call_score − put_score          ∈ [-100, +100]
contradiction     = min(call_raw, put_raw) / max(call_raw, put_raw)   (0 if both 0)

confidence_score  = |directional_score|/100
                    × (1 − w_contradiction · contradiction)
                    × data_quality_factor            (data_quality_score / 100)
                    × feature_coverage_factor        (available weight fraction)
                    × volatility_regime_factor       (penalizes abnormal regimes)
```

`CALL` and `PUT` are symmetric by construction (`call_raw − put_raw` is the
weighted mean of the signed family scores). `NO_TRADE` is a first-class result
with a machine-readable `no_trade_reason`, produced whenever evidence is too
close, confidence too low, data stale/insufficient, liquidity inadequate,
indicators contradictory, the session unsuitable, or the horizon unsupported.

**`confidence_score` is a bounded heuristic, not a calibrated probability.**
No `predicted_probability` or expected monetary value is produced in this
milestone.

## 7. Data honesty rules

* UTC timestamps internally; exchange-local timestamps preserved in outputs.
* Missing candles are **counted and flagged** — never forward-filled,
  interpolated, or imputed.
* Daily data is never presented as intraday; coarse data is never resampled to
  a finer interval.
* An in-progress candle is excluded unless explicitly configured otherwise.
* Stale latest candles (age > configured multiple of the interval) invalidate
  a live signal.
* Cache TTLs are interval-aware (a fraction of the candle interval), so
  intraday data never inherits multi-day cache assumptions.
* Stale cache is never presented as live data (`data_source` provenance field).

## 8. Run status

A run is one of `VALID`, `PARTIAL`, `DEGRADED`, `INVALID` — mapped from
provider coverage, data-quality failures, and validation results. A degraded
run is visually and machine-readably distinct from a valid one (banner +
`run_status` field in every output).

## 9. Out of scope (this milestone)

* Trade execution, broker connectivity, order placement, credentials.
* Stake sizing, martingale/loss-recovery logic.
* Calibrated probability models and expected-value math.
* Full historical replay / walk-forward backtesting (a small, isolated
  label-generation utility exists for future evaluation only).

These are documented as future milestones in `docs/BINARY_OPTIONS_ROADMAP.md`.
