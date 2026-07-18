# Repository architecture audit — pre-restructure baseline

Date: 2026-07-18. Baseline test suite: **266 passed** (pytest, offline).

## State before restructuring

Two research packages already exist and are logically independent
(`asset_selection`, `binary_options_selection`) on a shared generic layer
(`trading_common`: cache, rate limiter, IO helpers, transport error
taxonomy). The separation is clean at the *package* level but not at the
*repository* level:

| Concern | Finding |
| --- | --- |
| Configs | Both domains share the flat `configs/` root (`default_config.yaml`, `binary_options_config.yaml`) |
| Data | Both domains share `data/{raw,processed,cache}` — cache namespaces prevent key collisions but paths do not communicate ownership; equity and binary CSVs land in one directory |
| Reports | Both domains write into one flat `reports/` directory; binary runs overwrite prior runs silently |
| Docs | One flat `docs/` mixing equity docs, binary docs (`BINARY_OPTIONS_*`), shared policies, and historical audit notes |
| Tests | One flat `tests/` mixing equity, binary (`test_bo_*`), and shared-infra coverage |
| Root README | ~950 lines, mixes a landing page with the full equity manual |
| CI | None — no automated quality workflow |

## Domain-scope findings (binary options)

1. **Instrument availability is assumed, not verified.** The module signals
   on any symbol with market data; there is no notion of a platform contract
   catalog, per-expiration availability, contract status, or per-contract
   payout. A professional binary-options product must only mark a row
   actionable when a verified contract exists.
2. **Horizon grouping is wrong for the product definition.** The 30-minute
   horizon is currently classified `short` (5–30 inclusive); the product
   definition requires short_term = {5, 10, 15} and extended_intraday =
   {30, 60, 90, 120}.
3. **Provider delay is not modeled.** yfinance intraday data is delayed
   (~15 min); staleness gating exists but a *known structural delay* is not
   compared against the expiration horizon, so a 5-minute signal could pass
   freshness checks on data that is materially delayed relative to expiry.
4. **No `recommended_action` field** (BUY_CALL / BUY_PUT / NO_TRADE).
5. **No historical evaluation harness** — outcome label definitions exist
   (`signals/labels.py`) but nothing replays history or compares against
   baselines.
6. **Sessions are equity-only.** NY equity hours are applied regardless of
   asset type; forex/crypto/OTC sessions are unmodeled.
7. **No run manifest / run history** — reproducibility metadata (git commit,
   config hash, catalog hash) is not recorded and runs overwrite each other.

## Reuse decisions

Safe to reuse as-is: `trading_common` (verified free of domain imports),
candle validation, feature/indicator layer, symmetric directional scoring,
decision-gate pattern, run-status taxonomy, output-validation pattern,
StageStats, health probes, interval-aware cache TTLs.

Must remain domain-specific: configs, output paths, universe/catalog logic,
session rules, scoring weights, report renderers, error-reason taxonomies.

## Migration plan (implemented in the following commits)

1. Domain-separated `configs/`, `data/`, `reports/` paths with legacy-path
   fallbacks and deprecation warnings.
2. Domain-separated `docs/` (with `docs/INDEX.md`) and `tests/`
   (`common/`, `us_equities/`, `binary_options/`, `integration/`).
3. Horizon regrouping (30 → `extended_intraday`) and canonical signal
   terminology (`recommended_action`).
4. Instrument-catalog layer (typed model, YAML/CSV providers, validation,
   freshness, OTC policy) with `NO_TRADE / INSTRUMENT_AVAILABILITY_UNKNOWN`
   as the no-catalog default; generic watchlists demoted to explicit
   research mode.
5. Contract-aware pipeline: availability/expiration/payout/quote/delay
   gates, asset-type-aware sessions, expanded row schema.
6. Horizon-split outputs, complete horizon matrix, per-run artifact
   directories plus `latest/`, and a reproducibility run manifest.
7. Research-only historical evaluation framework with chronological splits
   and naive baselines.
8. Expanded domain/integration test coverage; root README replaced by a
   landing page; CHANGELOG; CI quality workflow (offline-capable).
