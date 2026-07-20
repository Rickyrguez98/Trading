# Provider reliability policy

Both modules treat every external data provider as unreliable by default and
prove otherwise per run.

## Shared mechanics (`trading_common` + per-domain layers)

- **Health probes** on benchmark symbols before real work; systemic failure
  refuses to produce a normal ranking/signal set rather than degrading
  silently.
- **Error taxonomy**: transport faults (rate-limited, timeout, blocked,
  parse, HTTP, unknown) are classified, counted, and distinguished from
  honest empty responses; provider-side failure ratios can invalidate a run.
- **Caching**: content-addressed JSON cache with per-domain directories.
  Equity data uses multi-hour/day TTLs; intraday data uses **interval-aware
  TTLs** (a fraction of the candle interval) and never inherits equity
  assumptions. Cache provenance (`live` / `fresh_cache` / `unavailable`) is
  recorded on every record; incomplete candles are never cached.
- **Rate limiting** per provider with configured minimum call gaps.
- **Diagnostics**: per-symbol provider outcomes, failure taxonomies, and
  fallback usage are published with every run
  (`provider_diagnostics.{md,json}`).

## Known provider characteristics

| Provider | Domain | Reality |
|---|---|---|
| yfinance (Yahoo) | both | unofficial, rate-limited, no SLA; intraday data typically **~15 min delayed** — never treated as real time (`known_delay_seconds`, `DATA_NOT_REAL_TIME` gate for short horizons) |
| Stooq | equities | keyless daily CSV fallback for prices |
| NASDAQ Trader / SEC | equities | universe sources; occasionally stale payloads, hence fallback |
| Manual YAML/CSV catalog | binary options | the only contract-availability source; freshness enforced via `catalog_timestamp` |

Replacing a provider means implementing the relevant interface
(`PricesProvider`, `IntradayCandlesProvider`,
`BinaryInstrumentCatalogProvider`) and registering it — business logic never
hard-codes provider behavior.
