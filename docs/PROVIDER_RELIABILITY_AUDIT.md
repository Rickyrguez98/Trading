# Provider Reliability Audit

_Scope: audit the asset-selection pipeline's data-provider layer and define a
robustness plan so the pipeline distinguishes **partial ticker failures** from
**systemic provider failures**, continues when possible, and **refuses to
present a trusted ranking when data coverage is insufficient**._

> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.

This document answers the five critical questions, records what the code does
**today** (commit baseline `49696fd`), and lays out the staged plan that the
follow-on commits implement.

---

## 1. The five critical questions (answered against current code)

### Q1 — Is there any real price provider besides yfinance?

**No.** `src/asset_selection/data_providers/__init__.py` registers exactly one
prices provider:

```python
_PRICES_REGISTRY = {"yfinance": YFinancePricesProvider}
```

There is no fallback. If Yahoo blocks or rate-limits the host (which is what we
observed live — even AAPL/MSFT/GOOGL returned `Expecting value: line 1 column 1`
JSON-parse errors), **every** price fetch fails and the pipeline has nothing to
fall back to.

**Plan:** add a provider-fallback interface and at least one practical,
free/keyless backup. Stooq (`https://stooq.com/q/d/l/`) is a CSV endpoint that
needs no API key and is suitable as a price/liquidity backup. Implemented as
`StooqPricesProvider`, wired behind a generic `FallbackProvider` that tries
providers in priority order (A → B → mark failure).

### Q2 — Is there any real fundamentals provider besides yfinance?

**No.** Only `YFinanceFundamentalsProvider` is registered. Fundamentals are
pulled from yfinance's `.info` / `.get_info()` blob, which fails together with
prices under a systemic Yahoo block.

**Plan:** keep yfinance as default but make the registry/abstraction clean so
SEC EDGAR (XBRL company facts), Finnhub, FMP, or Alpha Vantage can be plugged in
later **without paid keys required to run**. The generic `FallbackProvider`
works for fundamentals the moment a second provider is registered. A documented
implementation guide lives in `docs/DATA_SOURCES.md`.

### Q3 — Is news a separate provider, or also yfinance?

**Also yfinance.** `YFinanceNewsProvider` reads `yf.Ticker(symbol).news`, which
returns real, source-attributed, timestamped articles (publisher, link,
`providerPublishTime`). It is genuine news — but it shares yfinance's fate: a
systemic Yahoo block returns an empty list for every ticker.

Today, when news is empty the pipeline fills a **neutral** sentiment score
(50.0) with **confidence 0.0** and a `NO_NEWS` flag. That is honest about
confidence but does not *explicitly* mark sentiment as "unavailable due to
provider failure" vs. "genuinely no recent coverage".

**Plan:** when news fails systemically, mark sentiment **unavailable /
low-confidence** with an explicit reason, never silently neutral. Add a news
fallback interface (RSS / GDELT / Finnhub news) behind the same wrapper.

### Q4 — Does the report distinguish failure modes?

**Not granularly.** Records carry only `status ∈ {ok, empty, error}` plus a
free-text `error` string. `likely_no_data_reason()` is appropriately hedged (it
never asserts "delisted"), but there is **no machine-readable error taxonomy**,
so a JSON-parse error (provider down) looks the same as a genuinely unsupported
symbol.

**Plan:** add an explicit taxonomy in `data_providers/errors.py`:

```
OK
INVALID_TICKER              UNSUPPORTED_PROVIDER_SYMBOL
NO_PRICE_DATA               NO_FUNDAMENTAL_DATA          NO_NEWS_DATA
POSSIBLY_DELISTED
PROVIDER_RATE_LIMITED       PROVIDER_TIMEOUT             PROVIDER_BLOCKED
PROVIDER_JSON_PARSE_ERROR   PROVIDER_HTTP_ERROR
PROVIDER_EMPTY_RESPONSE     PROVIDER_UNKNOWN_ERROR
```

`classify_exception()` inspects the exception type/message (JSON decode → 
`PROVIDER_JSON_PARSE_ERROR`; HTTP 429 → `PROVIDER_RATE_LIMITED`; timeouts → 
`PROVIDER_TIMEOUT`; connection/blocked → `PROVIDER_BLOCKED`; etc.).
`classify_empty()` maps a successful-but-empty payload to `PROVIDER_EMPTY_RESPONSE`
or `NO_*_DATA`. Critically, an empty/JSON-parse response is **never** reported as
"possibly delisted"; delisting requires corroborating evidence.

### Q5 — Does the pipeline stop or downgrade ranking when coverage is too low?

**No.** The orchestrator returns `1` only when a stage produces an *empty*
DataFrame. There are no benchmark health checks, no coverage thresholds, and no
run-level validity status. A run where yfinance silently failed for 95% of the
universe would still emit a "normal" ranking off the surviving 5% — misleading.

**Plan:** add (a) **provider health checks** on benchmark mega-caps before the
full pipeline, (b) a **coverage validation layer** with configurable thresholds,
and (c) a **run-level `ranking_validity`** that downgrades or blocks the
ranking. See sections 3–4.

---

## 2. Root-cause finding (from the live run)

A custom run of `AAPL MSFT GOOGL NVDA BRK.B BF.B` failed for **every** ticker
with `Expecting value: line 1 column 1 (char 0)` — a JSON-parse error returned
by Yahoo's endpoint, identical for the most-liquid names on earth.

**Conclusion:** this is a **systemic provider failure** (Yahoo blocking/rate-
limiting the host), **not** a ticker-formatting problem and **not** evidence
that AAPL/MSFT are invalid or delisted. Separately, the class-share dot→hyphen
mapping already works correctly (`BRK.B → BRK-B`, `BF.B → BF-B`), with the
canonical spelling preserved end-to-end. So the issue is **provider-side**, with
the formatting layer already correct.

---

## 3. Gap summary

| Area | Today | Target |
| --- | --- | --- |
| Price providers | yfinance only | yfinance + Stooq backup behind fallback wrapper |
| Fundamentals providers | yfinance only | yfinance + pluggable interface (SEC EDGAR/Finnhub/FMP/AV) |
| News providers | yfinance only | yfinance + pluggable interface (RSS/GDELT/Finnhub) |
| Error detail | `status` + free text | machine-readable taxonomy + `error_type` field |
| Systemic vs. ticker | not distinguished | benchmark health check classifies systemic failures |
| Cache provenance | live vs cache hidden | `live` / `fresh_cache` / `stale_cache` / `fallback` / `unavailable` |
| Coverage gating | none | configurable thresholds + `ranking_validity` |
| Run status | exit 0/1 only | `VALID` / `PARTIAL` / `DIAGNOSTIC_ONLY` / `INVALID_*` |
| Diagnostics report | none | `reports/provider_diagnostics.{md,json}` |
| Health-check CLI | none | `--health-check-only`, `--no-provider-health-check`, ... |

---

## 4. Robustness plan (mapped to commits)

1. **audit** — this document.
2. **provider health checks** — `health/provider_health.py`: probe benchmark
   tickers (AAPL, MSFT, GOOGL, NVDA, BRK.B) for price/fundamentals/news; emit a
   per-check record and a systemic-failure classification.
3. **error classification** — `data_providers/errors.py` taxonomy; wire
   `error_type` into `PriceSnapshot` / `Fundamentals` and the live providers;
   aggregate by error type in stage stats.
4. **fallback + cache backup** — `FallbackProvider`, `StooqPricesProvider`,
   cache provenance + a "serve fresh-enough cache on live failure" policy with
   explicit config (`use_cache_on_provider_failure`, `max_cache_age_days`,
   `provider_priority_by_data_type`, ...).
5. **coverage validation** — `validation/coverage.py`: count valid/failed by
   data type, compute coverage ratios, compare to thresholds, derive
   `ranking_validity`. Block or downgrade accordingly.
6. **diagnostic-only run status + reports** — `validation/provider_diagnostics.py`
   writes `reports/provider_diagnostics.{md,json}`; the orchestrator labels
   `top_candidates.md` and `asset_selection_summary.json` with the run status and
   never presents a low-coverage run as a valid ranking.
7. **docs** — README run-statuses, failure types, backup plans, health-check
   usage, and how robustness supports future allocation/rebalancing.
8. **tests** — health checks, systemic-failure detection, fallback sequence,
   stale-cache provenance, coverage gating, diagnostic output, class-share
   normalization, JSON-parse classification, run-status surfacing.

### Backup-plan ladder

- **Plan A** — primary provider per data type (default: yfinance).
- **Plan B** — secondary/fallback provider (prices: Stooq; others: pluggable).
- **Plan C** — fresh-enough cache (within `max_cache_age_days`), labeled
  `stale_cache`, used only when live + fallback fail and the policy allows it.
- **Plan D** — diagnostic-only: live + cache exhausted → write diagnostics, do
  **not** present a valid ranking.

### Critical rules (non-negotiable)

- Never fabricate data; never hide provider failures.
- Never call a systemic yfinance failure a ticker problem; never call AAPL/MSFT
  invalid.
- Never emit a normal ranking when provider coverage is too low.
- Never silently use stale cache as if it were live.
- No live trading, brokerage integration, or asset allocation in this milestone.
