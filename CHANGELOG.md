# Changelog

All notable changes to this repository. Dates are UTC.

Versions use a single **0.0.N** pre-release counter: this is research
software and the earlier 0.x.0 scheme overstated its maturity. The entries
below were previously numbered 0.1.0-0.4.0 and have been renumbered onto
this counter; releases before 0.0.12 were never tracked in this file.

## [Unreleased]

### Added — prognosis, frozen table, orders spreadsheets, my-stocks analysis
- **Prognosis column** per lot: "waiting looks worthwhile" or "likely to fall"
  only when a trend passed its back-test, otherwise "no reliable direction"
  with the unproven lean; the expected price and its date, when the middle
  scenario reaches break-even, and the chance of halving within a year.
- **Frozen main columns** on the portfolio table (stock to days held); the
  prognosis and the 1-month to 1-year forecasts scroll sideways beneath them.
  The portfolio page uses the full window width.
- **Orders from a spreadsheet** (CSV or Excel), English or Spanish headers,
  day-first dates, sign-based sides; and an upload box for trades at the top
  of the dashboard.
- **My stocks only**: run the selection pipeline (fundamentals, news, scores)
  on just your holdings, shown on the Portfolio page.

### Changed
- Forecast back-tests use up to ten years of monthly tests (about twelve years
  of prices), so 3-, 6- and 12-month calls can be judged for stocks with the
  history; stocks without it say how many years they would need.
- The validation confidence interval now counts overlapping tests as the
  independent evidence they are (it treated 24 monthly tests of a 3-month
  call as 24 independent ones, overstating certainty).

### Fixed
- Live prices: the app now sends Yahoo one request at a time, and when Yahoo
  answers empty three times in a row (a rate limit, reported by yfinance as
  "possibly delisted") it pauses for five minutes and shows the last known
  prices instead of retrying every stock every five minutes.

### Changed — forecast by horizon and a short-term recommendation
- The portfolio's single "next month" column becomes four groups — 1 month,
  3 months, 6 months, 1 year — each with trend (up / down / flat and the
  share of simulated futures ending higher), expected (median), min and max,
  followed by a short-term recommendation per lot.
- The model keeps each stock's own trend, shrunk by its reliability
  (`max(0, 1 - 1/t^2)`), and every trend shows its walk-forward record; a ✓
  needs a better-than-coin-flip record at 95% confidence over at least eight
  independent periods, so 6- and 12-month calls are marked as not testable.
- The recommendation tries sixty limit prices and picks the best trade-off
  between price and the chance of reaching it within a month (at least a 25%
  chance), with a protective stop hit in about one month in ten; it uses a
  trend only if that trend is validated at one month.
- About six years of prices are fetched for this so the longer calls can be
  tested; the CSV carries every new column.

### Fixed
- A cached price history written before its start date was recorded was
  treated as covering any period, so a longer history was never fetched for
  it; it now counts only from the first date it holds.

### Added — next month's range and sell levels on the portfolio
- The operations table gets a last column for every lot still held: the
  expected range for the next month (average simulated high and low), a sell
  limit reached in about half of the simulated months, and a stop reached in
  about a quarter of them, each also shown against what that lot cost. Built
  on the monitor's demeaned block bootstrap (2,000 paths, 21 trading days), so
  it describes volatility, not direction, and the page says so. Each stock's
  range is back-tested walk-forward ("range held n of m past months");
  recent listings are shown as not back-tested. Also in the CSV and at
  `/api/portfolio/outlook`; computed once per trading day.

### Added — portfolio from trade confirmations
- **Portfolio page.** Import any number of trade confirmation PDFs at once
  (GBM's US-dollar account, issued by DriveWealth). Every trade line is read
  with its own amounts and cross-checked; unreadable lines are reported;
  re-uploading a file adds nothing. Calibrated on 16 real confirmations
  (53 executions): all read, no warnings.
- Executions are merged back into orders (GBM fills whole and fractional
  shares separately) and sales are matched to purchases first in, first out.
  One row per purchase lot: buy date and price, cost with commission, sale
  date and price and what was received, or — for lots still held — the live
  price and the result if sold now; gains green, losses red. Sales without an
  imported purchase and fractional rounding residue are flagged, not hidden.
- Open lots are repriced every five minutes (Yahoo is asked at most once per
  five minutes per stock); headline tiles show market value, open and
  realized results and commissions.
- Charts of every stock traded, starting before the first purchase, with
  buy/sell markers and the average cost of open positions; sector view of
  your stocks against the S&P 500.
- Downloads: the operations table and the raw executions as CSV.
- "Use as my holdings" writes the open lots to the monitor's holdings file
  (option B, with a backup), and the dashboard's default watchlist becomes
  your own open positions.
- New module `asset_selection.monitoring.confirmations` (PDF reader) and
  `asset_selection.monitoring.ledger` (orders, FIFO matching, holdings,
  ledger CSV), both usable without the app.

### Added — NewsAPI.ai news provider
- `newsapi_ai` (Event Registry) searches headlines by company name; the key is
  read from `NEWSAPI_AI_KEY` and sent in the request body. It is first in the
  shipped news chain (`newsapi_ai, finnhub, yahoo_rss`) and skipped at no cost
  without a key. To protect the 2,000-token free plan, answers are cached for
  24 hours and a run makes at most `NEWSAPI_AI_MAX_CALLS` searches (default
  200). Tested against a stubbed HTTP layer; live responses not yet exercised.

### Added — app dashboard v2 ("Finance Bull")
- **Market charts on the dashboard.** A year of daily candles for every symbol
  in a watchlist (default: your holdings, then the top eligible stocks of the
  ranking), each with SMA 50/200, Bollinger bands (20, 2σ), Fibonacci
  retracement levels of the year's swing, volume and an RSI (14) pane, plus a
  statistics strip (52-week range, volatility, drawdown, trend, Fibonacci zone,
  ATR). A **By sector** view rebases every stock to 0%, adds an equal-weight
  sector index and the S&P 500, and reports each sector's lead or lag.
  Symbols are added and removed on the page (validated against Yahoo, saved in
  `data/app/settings.json`). New API: `/api/market/watchlist`,
  `/api/market/chart/{symbol}`, `/api/market/sectors`. The studies are labelled
  as descriptive, not forecasts.
- Prices are cached for six hours and fetched one at a time; a chart request
  has its own short rate-limit policy, so a page never waits minutes.
- Charts use TradingView Lightweight Charts 5.2.1 (Apache-2.0), vendored with
  its licence and pinned by hash; nothing is loaded from a CDN.
- **Professional redesign** for presentations: Finance Bull logo and favicon,
  navy / ivory / gold theme taken from the logo, serif headings, headline
  tiles, light and dark mode with a switch, phone-width layout.
- The selection job page shows a progress bar per stage and highlights
  rate-limit pauses; the selection page explains how pauses work and how long
  a full run takes.

### Fixed — app
- Finished job processes are now collected (they were left as zombies until
  the app exited, which also showed up as a `ResourceWarning` in tests).

### Fixed — rate limiting ("429 Too Many Requests")
- A 429 used to be retried three times within about three seconds and then
  given up on, ticker after ticker. On 2026-10-15 the Yahoo news feed answered
  429 to all 150 shortlisted tickers: ~16 minutes of retries and 150 warnings
  for no data. Every provider call now goes through a shared throttle
  (`trading_common.throttle`): requests are spaced (yfinance 0.4 s, Yahoo news
  2 s, Finnhub 1.1 s); a 429 pauses that provider -- for as long as
  `Retry-After` asks, otherwise with escalating pauses -- and then continues;
  repeated 429s pause the provider for a cooldown so its calls are skipped
  instead of hammering it, followed by a single probe.
- Measured live: a 12-ticker run that hits the blocked news feed takes one
  5-second pause and logs two warnings; prices and fundamentals stay 12/12.
- Stage 4 logs a skipped ticker at debug level while its provider is paused,
  and every run ends with a rate-limiting summary line.
- Quick automatic retries (tenacity) now skip rate-limit errors, which only the
  throttle handles, so a 429 is no longer answered with an immediate retry.
- Prices and fundamentals never skip a ticker because of a pause: when Yahoo
  keeps answering 429 the run waits out the 10-minute cooldown and carries on
  with the same ticker. Only after three cooldowns in a row without one
  successful call (about an hour -- a block, not a throttle) does it stop
  waiting and report the remaining tickers as rate-limited, so a run can never
  hang. News, which is best-effort, still fails fast while paused.

### Fixed
- **`--instrument-price` silently discarded all but the last value.** The flag
  used `nargs="+"` without `action="extend"`, so argparse *overwrote* on repeat:
  `--instrument-price VOO=715.59 --instrument-price BND=70.22` kept only BND.
  The index core — the largest sleeve in every strategy — was then left with no
  price and no share count, and the plan quietly parked that money in cash. On
  a balanced $25,000 plan it left $8,587 (34% of the portfolio) uninvested
  while reporting `PRICE_UNAVAILABLE_NO_SHARE_COUNT`, which reads like a
  provider fault rather than a dropped argument. The existing test covered the
  string-to-dict helper but never the argparse wiring, which is why it slipped
  through; the new test covers both spellings and the mixed form.
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
- Suite at **408 offline deterministic tests** (19 new), verified on
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

### Added — Broker options A / B / C
- The monitor reads holdings through a selectable broker option:
  **A** statement PDF, **B** holdings CSV (default), **C** Interactive Brokers
  Flex Web Service (read-only). Choose with `broker.option` in
  `configs/us_equities/monitoring.yaml` or `trading-monitor --broker A|B|C`;
  `--list-brokers` shows which options are ready; `--init-holdings` creates
  the personal CSV under `data/` (git-ignored).
- `asset_selection.monitoring.ibkr_flex`: SendRequest / GetStatement with
  polling for "statement in progress" codes, explained error codes, and lot-
  level open dates. The token comes from the environment only, is scrubbed from
  every error message, is never logged, and is only sent to
  `*.interactivebrokers.com`; the raw statement (with the account ID) is never
  saved. Shorts, options and non-USD/MXN positions are skipped and listed.
  Tested against synthetic responses built on the Flex XML schema; not yet run
  against a live account.
- The monitor now loads `.env` like the selection CLI, so keys kept there work.

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

### Added — Trading app (first structure)
- `trading-app` (`trading_app`): a local web app over the whole US-equities
  workflow — dashboard, selection (full or sample), allocation, broker option
  picker with holdings-file creator and uploader, monitor results with evidence
  and outlook — plus a JSON API documented at `/api/docs`.
- Layers: `trading_app.services` is plain Python (no web framework) and runs the
  same pipeline commands the CLIs document, as background jobs with logs and
  exit codes on disk; `trading_app.web` is FastAPI with server-rendered pages.
  A sample selection run stays in its job folder and can never replace the
  full-universe ranking.
- Local-only by design: loopback binding, a localhost Host-header check (DNS
  rebinding), refusal of cross-origin writes, validated argument lists instead
  of shell strings, checked uploads, escaped output, no credentials accepted.
- New optional extra `app` (FastAPI, uvicorn, Jinja2, python-multipart), with
  version markers: current releases of those need Python >= 3.10.

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
