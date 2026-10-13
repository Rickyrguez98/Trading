# Changelog

All notable changes to this repository. Dates are UTC.

Versions use a single **0.0.N** pre-release counter: this is research
software and the earlier 0.x.0 scheme overstated its maturity. The entries
below were previously numbered 0.1.0-0.4.0 and have been renumbered onto
this counter; releases before 0.0.12 were never tracked in this file.

## [Unreleased]

### Fixed
- **A pandas NaN reaching the JSON report.** Optional text fields were
  serialized with `row.get(x) or None`, and NaN is truthy in Python, so on
  pandas >= 2.0 (which coerces a missing object value to NaN) a bare `NaN`
  literal landed in the candidate payload — invalid JSON, rejected by strict
  parsers. New `_safe_str` mirrors the existing `_safe_num` NaN guard across
  all eight optional text fields; `reason` previously had no guard at all.
  This was also why CI was red on Python 3.11 and green on 3.9.
- **Empty sentiment.** News was silently producing nothing: Yahoo's private
  per-ticker endpoint behind the `yfinance` news provider has been retired
  upstream and answers HTTP 404, so every name scored neutral with a zero
  article count. News now defaults to a chain, `[finnhub, yahoo_rss]`, and
  sentiment is confirmed to populate through the stage-4 path.

### Added
- `finnhub` news provider (`asset_selection.data_providers.news_provider`):
  the documented `/company-news` API, now the default. It is the only news
  backend here whose terms permit automated access, and it returns a real
  per-article publisher so the source-diversity term in the
  sentiment-confidence model is meaningful instead of inflated. Needs a free
  key in `FINNHUB_API_KEY`, sent in the `X-Finnhub-Token` header rather than a
  query parameter so it cannot leak into URLs, logs, or cache keys.
- `yahoo_rss` news provider: keyless RSS headline feed, parsed with the
  standard library (no new dependency), chained behind `finnhub` so sentiment
  still has a route to real articles with no key configured.
- `NewsProviderNotConfigured`: a missing credential raises **before** any
  network call and is never swallowed, so a configuration fault cannot
  masquerade as "this company has no news". In a chain the next provider takes
  over; as the sole provider the run reports news unavailable.

### Changed
- `docs/us_equities/data-sources.md`: the News section rewritten, and the
  "Rate limits & terms of service" section corrected. It previously claimed the
  pipeline "never bypasses terms of service" while the Yahoo hosts behind
  `yfinance` and `yahoo_rss` serve `robots.txt: Disallow: /`. The real position
  is now documented, along with the throttling observed on the RSS host
  (persistent HTTP 429 from datacenter IPs while price calls on the sibling
  host still succeed).
- Google News RSS was evaluated and **rejected on policy grounds, not
  capability**: it is reachable and returns real headlines, but its
  `robots.txt` disallows the feed path for all agents and names automated
  crawlers explicitly. Reachability is not permission.

### Testing
- Suite at **407 offline deterministic tests** (18 new), verified on
  Python 3.9 / pandas 1.5.3 **and** Python 3.11 / pandas 3.0.6: Finnhub parsing, the
  header-not-URL key contract, missing/blank key behaviour with no network
  call, HTTP 401/403/429/5xx and error-payload handling, honest empty results,
  and the keyless fall-through from `finnhub` to `yahoo_rss`.

### Known limitation
- Live Finnhub responses are **not** exercised here — that needs a personal API
  key. The contract is tested; real coverage and latency are not claimed.
  `yahoo_rss` could not be verified live either: every attempt from the
  development environment returned HTTP 429.

### Added — Portfolio monitor (US Equities Milestone 3)
- `trading-monitor` (`asset_selection.monitoring`): SELL / TRIM / HOLD / ADD /
  REVIEW for the positions you already own, with share counts, money, the
  evidence behind each call and an outlook. Inputs: the holdings CSV template
  or a brokerage statement PDF (GBM / DriveWealth vocabulary, optional
  `[pdf]` extra), written back out as an auditable CSV.
- Valuation in MXN or USD; SIC returns split exactly into the stock's dollar
  return and the USDMXN move, converting each lot at its own purchase date.
- Thesis rules about the business, not the price: read from a full-universe
  ranking (fundamentals scores are cross-sectional and meaningless when a
  dozen stocks are scored alone); price-driven exclusions never break a
  thesis; missing data is UNKNOWN; index and bond funds are STRATEGIC and only
  rebalanced. A loss alone, or a gain alone, never triggers a sale; ADD and
  TRIM both wait for the rebalance band.
- Forecasting: a demeaned block bootstrap (no directional view) and the
  optional Chronos-2 deep-learning model (`[forecast]` extra, Python >= 3.10).
  Each is validated per stock against a random walk (pinball loss,
  Diebold-Mariano with HLN correction and Newey-West variance), with an
  independent-window guard and Benjamini-Hochberg control across the run. A
  forecast can influence an action only if it is directional and survives all
  of that.
- `trading-us-equities --processed-dir`: `--output-dir` moved only the
  reports, so a small custom run would have overwritten the main ranking.
- Docs: `docs/us_equities/monitoring.md`, and
  `docs/us_equities/broker-integration-options.md` (step 3 research: GBM has
  no client API; Ley Fintech Art. 76 customer-data rules are still
  unpublished; IBKR Flex Web Service is the official read-only route).

### Found by running on real data (fixed before release)
- A dozen-stock rescore graded holdings against each other and recommended
  selling half a healthy portfolio, including the S&P 500 index fund.
- Chronos produced negative price quantiles at long horizons; `log()` turned
  them into NaN and silently erased that stock's validation.
- One stock "beat the random walk" at p = 0.0037 — about what luck predicts
  across 24 tests; it does not survive the run-level correction.

### Testing
- 192 new tests (600 in total), passing on Python 3.9 / pandas 1.5.3 and on
  Python 3.11 / pandas 3.0.6 with Chronos installed. The validation is tested
  from both sides: no false skill on pure random walks, and real skill
  detected for an oracle that sees the future.

## [0.0.15] — 2026-10-10 — Portfolio allocation (US Equities Milestone 2)

### Added
- `asset_selection.allocation`: turns the selection ranking plus a capital
  amount into three risk-profiled portfolios (conservative / balanced /
  aggressive), each a sleeve budget summing to 100% over cash, a bond proxy,
  an S&P 500 index core, quality single names, growth satellites and an
  opt-in hard-capped speculative sleeve. Equity sleeves are filled from the
  ranking by `candidate_role`, so selection drives allocation.
- Weighting: `score ** (1 - tilt) * volatility ** (-tilt)` normalized to the
  sleeve budget, reusing the `allocation_adjusted_score` selection already
  computed (fundamentals + confidence-adjusted news − risk) without
  re-scoring; the inverse-volatility tilt varies by strategy.
- Concentration control: per-name cap inside each sleeve plus a
  portfolio-wide per-sector cap across all single names (business-type
  diversification), with unplaceable weight overflowing to the broad index
  core and reported via named warnings (`EMPTY_SLEEVE`, `SLEEVE_SATURATED`,
  `SECTOR_CAP_OVERFLOW`). Infeasible designs are rejected at config load.
- Money layer: whole-share counts floored against the commission-inclusive
  unit cost so a plan can never overspend; money conserved as
  invested + commission + cash; un-investable targets and unaffordable share
  prices reported rather than zeroed silently.
- Trust gate: refuses to plan on a selection run with status
  DIAGNOSTIC/INVALID unless explicitly overridden; a missing selection
  verdict degrades the run. Instrument prices come from the provider or from
  user-supplied `--instrument-price` values — never invented.
- CLI `trading-portfolio` and `make portfolio`; outputs in
  `data/us_equities/processed/portfolio_*.csv` and
  `reports/us_equities/portfolio/` (Markdown with percentage **and** money,
  order ticket, sector diversification; plus JSON summary).
- Docs: `docs/us_equities/allocation.md` (full methodology) and
  `allocation-readiness.md` (the architecture decision, including what was
  deliberately rejected from a prior deep-learning trading project).

### Testing
- Suite grown to **370 offline deterministic tests** (49 new), covering
  config validation, eligibility and sleeve assignment, weighting at both
  tilt extremes, both caps and every overflow path, money conservation, the
  trust gate, risk monotonicity across the three strategies, and the CLI.

### Deferred
- Mean-variance / risk parity / Black-Litterman, portfolio-level risk metrics
  (VaR, drawdown, correlation) and drift-based rebalancing against existing
  holdings — all need inputs this repository does not yet produce honestly.

## [0.0.14] — 2026-07-19 — Professional platform restructure

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

## [0.0.13] — 2026-07-18

- Binary-options research module (intraday signals, candle validation,
  horizon-aware scoring, NO_TRADE-first decisions, output validation).
- `trading_common` shared infrastructure extracted (cache, rate limiter,
  IO, transport error taxonomy) with backward-compatible shims.
- US Equities: optional FinBERT sentiment (comparison/ensemble modes) with
  anti-fabrication guarantees.

## [0.0.12]

- Initial US Equities asset-selection research pipeline (staged funnel,
  provider health checks, coverage gating, diagnostics, output validation).
