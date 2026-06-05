# Architecture

## Design goals

1. **Explainability.** Every ranking decision is traceable to sub-scores and
   the raw fundamentals/sentiment that produced them. No black-box ML on the
   final ranking.
2. **Replaceable data layer.** No business logic depends on a specific vendor.
   Providers implement a common base class; swapping `yfinance` for Finnhub or
   FMP only touches `data_providers/`.
3. **Honest missing data.** Missing fields are surfaced in the output and
   penalized in the score. The pipeline never imputes values silently.
4. **Free-by-default.** The default pipeline runs without any API keys.
5. **Extensible toward allocation.** The same score/flag schema is exactly
   what an allocation layer would consume next.

## Layered structure

```
┌──────────────────────────────────────────────────────────────┐
│ pipelines/run_asset_selection.py        ← CLI orchestrator   │
└─────────────────────────┬────────────────────────────────────┘
                          │
   ┌──────────────────────┼──────────────────────────────┐
   │                      │                              │
   ▼                      ▼                              ▼
universe.py        data_providers/                  scoring/
                   ├─ fundamentals_provider           ├─ composite_score
                   ├─ prices_provider                 └─ ranking
                   └─ news_provider                       │
                          │                               │
                          ▼                               ▼
                   sentiment/                       fundamentals/
                   ├─ text_preprocessing           ├─ growth_metrics
                   └─ sentiment_model              ├─ quality_metrics
                                                   ├─ valuation_metrics
                                                   └─ fundamental_scoring
```

## Provider interface

All data providers inherit from `data_providers.base.DataProvider`. They
expose a single public method (`get_*`) and share:

- a `Cache` instance for on-disk JSON caching;
- a `RateLimiter` for polite request pacing;
- structured logging via the project's `logging_config`.

To add a new provider:

1. Subclass `FundamentalsProvider` / `PricesProvider` / `NewsProvider`.
2. Implement the abstract `fetch_*` method.
3. Register the provider name in `data_providers/__init__.py`.
4. Reference it by name in `configs/default_config.yaml` under `providers:`.

## Scoring pipeline

```
raw fundamentals + prices ──► metric extraction (per-pillar)
                              │
                              ▼
                       winsorize + z-score across universe
                              │
                              ▼
                       map to [0, 100] sub-scores per pillar
                              │
                              ▼
                       aggregate -> fundamentals_score
news headlines ──► VADER (or FinBERT) ──► aggregate -> sentiment_score
                              │
                              ▼
       composite = Σ wᵢ · scoreᵢ − w_risk · risk_penalty
                              │
                              ▼
                       rank + flag + write outputs
```

## Caching

Cache keys are content-hashed by `(provider, method, args)` and stored under
`data/cache/` as JSON. Each provider declares a TTL in config. A
`--refresh-cache` CLI flag invalidates the cache for the current run.

## Future extension points

- `allocation/` — given the ranked candidates + a risk model, output
  position sizes (e.g. equal-weight top-K, risk-parity, MVO).
- `backtesting/` — historical replay of the selection rules with realistic
  rebalancing cadence.
- `risk/` — per-name and portfolio-level risk metrics (vol, drawdown, factor
  exposures).

These directories already exist with documented intent but the asset-selection
pipeline does not import them.
