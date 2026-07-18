# Binary Options Selection — Data Sources

The intraday module uses only free/public data by default and documents the
gaps instead of papering over them.

## Intraday OHLCV candles

### Default: yfinance (Yahoo Finance, unofficial)
- Intervals used: provider-native `1m, 2m, 5m, 15m, 30m, 60m/1h`. Coarse data
  is **never** resampled into a finer interval and daily bars are never
  presented as intraday.
- **Delay:** Yahoo intraday quotes are typically delayed (~15 minutes for many
  venues) and carry no SLA. The pipeline measures `data_age_seconds` per row
  and the staleness gate (`3 × interval` by default) turns stale rows into
  `NO_TRADE (STALE_DATA)` instead of pretending the tape is live. With the
  default 5m interval and Yahoo's delay, expect rows to sit near the staleness
  boundary — that is honest, not a bug.
- **History limits:** ~7 days back for `1m`; ~60 days for other intraday
  intervals. The provider requests only what the run needs.
- Class shares use dot→hyphen mapping (`BRK.B` → `BRK-B`).
- `auto_adjust=False` is passed explicitly and the adjusted status is recorded
  on every series.

### Replaceability
`IntradayCandlesProvider` is an ABC with a registry
(`binary_options_selection/data_providers/__init__.py`). Adding a keyed
provider (Polygon, Alpaca Data, Finnhub, Twelve Data, …) or a future
broker-availability provider means subclassing, registering, and naming it in
`config.IMPLEMENTED_PROVIDERS` — no pipeline or scoring changes. Fallback
chains (`providers.priority`) may only name genuinely implemented providers
(validated).

## Caching

Shared `trading_common.cache` JSON cache with **interval-aware TTLs**:
`ttl = clamp(interval_seconds × 0.5, 15 s, 900 s)`. Intraday data never
inherits the multi-day TTLs of the equity fundamentals pipeline. In-progress
candles are never written to the cache, and cached data is labeled
`fresh_cache` — stale cache is never served as live data.

## Universe

No assumption that every U.S. stock is listed as a binary-options underlying:
- `custom` — explicit tickers;
- `watchlist` — YAML list or a text file (safest default);
- `asset-selection` — the equity module's output CSV as an upstream
  liquidity/quality filter **only** (fundamentals are not an intraday
  predictor); optionally carries its `sentiment_score` for extended horizons.
- future: a broker-specific availability provider (no broker connectivity in
  this milestone).

## Benchmark / market context

Default `SPY` at the same candle interval, fetched through the same provider.
If it is unavailable or stale the context family is reported missing and the
run is marked `DEGRADED` — never silently ignored.

## Market sessions

Structural classification from the exchange-local clock
(`America/New_York` by default; regular 09:30–16:00, pre-market from 04:00,
after-hours to 20:00). **No exchange holiday calendar** in this milestone —
on holidays the staleness gate is what protects the signal (documented in
[`limitations.md`](limitations.md)).

## Rate limits & terms

Same guest policy as the equity module: on-disk caching of every response,
minimum-gap rate limiting per provider, no HTML scraping, no ToS bypass.
