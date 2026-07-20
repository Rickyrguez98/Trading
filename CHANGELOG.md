# Changelog

All notable changes to this repository. Dates are UTC.

## [0.3.0] — 2026-07-19 — Professional platform restructure

### Repository
- Domain-separated layout: `configs/`, `data/`, `reports/`, `docs/`, and
  `tests/` each split into `us_equities` / `binary_options` (+ `shared`,
  `common`, `integration`); caches can no longer collide. Legacy flat paths
  keep working through deprecation shims
  ([docs/shared/migration.md](docs/shared/migration.md)).
- Root README replaced with a professional landing page; the full equity
  manual moved to `docs/us_equities/README.md`; new documentation set under
  `docs/` with `docs/INDEX.md` navigation.
- Canonical CLIs `trading-us-equities` and `trading-binary-options`
  (previous names kept as aliases); Makefile quality/run targets; GitHub
  Actions offline quality workflow; CHANGELOG introduced.

### Binary Options — contract-aware scope correction
- **Instrument catalog layer**: typed `BinaryInstrument` model, replaceable
  catalog provider interface with manual YAML/CSV providers, schema
  validation and `--validate-catalog-only`, freshness enforcement, OTC
  policy (excluded by default), per-expiration payout and platform-quote
  data. Without a verified catalog every row is
  `NO_TRADE / INSTRUMENT_AVAILABILITY_UNKNOWN`; generic watchlists moved
  behind explicit `--research-mode`.
- **Horizon regrouping (breaking)**: `short_term` = 5/10/15 min;
  `extended_intraday` = 30/60/90/120 min (30 moved out of short);
  `weights.short` deprecated in favour of `weights.short_term`.
- **Canonical terminology**: `recommended_action` (`BUY_CALL` / `BUY_PUT` /
  `NO_TRADE`) and the canonical NO_TRADE reason taxonomy (contract,
  market-data, session/timing, and evidence gates) including
  `DATA_NOT_REAL_TIME` (known provider delay vs horizon — default yfinance
  ~900 s delay honestly rejects short-term signals),
  `TIME_TO_EXPIRY_TOO_SHORT`, `QUOTE_MISMATCH`, `PLATFORM_QUOTE_STALE`,
  payout gates, and `SESSION_NOT_SUPPORTED`.
- **Asset-type-aware sessions** (crypto 24/7, forex 24/5 approximation,
  equity/ETF/index exchange hours, unmodeled types refused) and asset-type-
  aware liquidity gating; provider symbol mappings from the catalog
  (`EURUSD=X`, `BTC-USD`).
- **Professional outputs**: horizon-split CSVs and Markdown reports, the
  complete instrument × horizon decision matrix, immutable
  `reports/binary_options/runs/<run_id>/` history with a refreshed
  `latest/`, and a reproducibility `run_manifest.json` (git commit, config
  and catalog hashes, provider versions — no secrets).
- **Historical evaluation framework** (research-only): chronological
  replay with structural anti-leakage, time-based splits, honest outcome
  labels (`UNKNOWN` ≠ loss), explicit payout assumptions, naive baselines,
  and verdicts that refuse predictive-power claims without out-of-sample
  evidence.

### Testing
- Suite grown to **320 offline deterministic tests**, including catalog,
  contract-gate, matrix/manifest, evaluation-leakage, domain-separation,
  and backward-compatibility coverage.

## [0.2.0] — 2026-07-18

- Binary-options research module (intraday signals, candle validation,
  horizon-aware scoring, NO_TRADE-first decisions, output validation).
- `trading_common` shared infrastructure extracted (cache, rate limiter,
  IO, transport error taxonomy) with backward-compatible shims.
- US Equities: optional FinBERT sentiment (comparison/ensemble modes) with
  anti-fabrication guarantees.

## [0.1.0]

- Initial US Equities asset-selection research pipeline (staged funnel,
  provider health checks, coverage gating, diagnostics, output validation).
