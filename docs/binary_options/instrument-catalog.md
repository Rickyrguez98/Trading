# Instrument catalog — schema, validation, and platform adapters

The catalog is the **only** source of contract availability, expirations, and
payouts. No platform is integrated with this repository, so the working
providers read a **manual YAML or CSV file you maintain from your platform's
actually listed instruments**. Example entries
([../../configs/binary_options/instruments.example.yaml](../../configs/binary_options/instruments.example.yaml))
are illustrative and never live contracts.

## YAML schema

```yaml
platform: my_platform                       # required identifier
catalog_timestamp: "2026-07-18T13:00:00Z"   # REQUIRED: when YOU captured this
instruments:
  - platform_asset_id: AAPL-BIN             # required, unique per catalog
    underlying_symbol: AAPL                 # required
    provider_symbol: AAPL                   # market-data mapping (EURUSD=X, BTC-USD, ...)
    asset_type: equity                      # equity|etf|forex|index|commodity|cryptocurrency
    platform_asset_name: "Apple"            # optional display fields...
    contract_id: null
    underlying_name: "Apple Inc."
    market: NASDAQ
    currency: USD
    available_now: true
    contract_status: open                   # open | closed | suspended
    available_expirations_minutes: [5, 10, 15, 30, 60]
    payout_by_expiration: {5: 0.80, 15: 0.85, 60: 0.87}   # NET profit per unit staked
    payout_timestamp: null                  # defaults to catalog_timestamp
    minimum_trade: 1
    maximum_trade: 1000
    platform_reference_price: 199.42        # optional platform quote
    reference_price_timestamp: "2026-07-18T12:59:55Z"
    quote_source: my_platform
    exchange_timezone: America/New_York
    is_otc: false
```

CSV columns mirror these fields; list fields use `|` separators (`5|10|15`)
and payout uses `expiration:ratio` pairs (`5:0.80|15:0.85`); each row carries
`catalog_timestamp`.

## Validation

```bash
trading-binary-options --validate-catalog-only --instrument-catalog PATH
```

Fatal problems (exit 2, run would be INVALID): empty catalog, missing or
unparseable `catalog_timestamp`, duplicate `platform_asset_id`, missing
`platform_asset_id`. Per-instrument issues (only that instrument's rows are
excluded, with the reason recorded): missing symbol mapping, unsupported
asset type, expirations outside 5–120 minutes, implausible payouts (≤0 or
>5), payouts for unavailable expirations, non-positive platform quotes.
Warnings: catalog older than `instrument_catalog.max_catalog_age_seconds`
(every row becomes `NO_TRADE / CATALOG_STALE`), instruments without payout
data.

## Freshness, economics, and quote-alignment gates

Configured under `instrument_catalog:` in
[default.yaml](../../configs/binary_options/default.yaml):
`max_catalog_age_seconds`, `exclude_otc` (default **true**; allowed OTC rows
always carry an `OTC_CONTRACT` warning), `minimum_acceptable_payout`,
`maximum_payout_age_seconds`, `max_quote_diff_bps`,
`max_platform_quote_age_seconds`. When the catalog carries a platform quote,
the pipeline compares it against the market-data reference price and reports
`absolute_quote_difference`, `relative_quote_difference_bps`, and
`quote_alignment_status` (`aligned` / `mismatch` / `stale` / `unavailable`);
divergence beyond the limit is `NO_TRADE / QUOTE_MISMATCH` — the market-data
price is never assumed to equal the platform settlement reference.

## Platform adapter interface (future broker-specific integration)

```python
class BinaryInstrumentCatalogProvider(ABC):
    name: str
    def get_available_instruments(
        self, *, as_of: datetime | None = None, platform: str | None = None
    ) -> CatalogSnapshot: ...
```

A future adapter for a specific platform implements this interface (returning
`BinaryInstrument` entries with real availability/payout/quote data), gets
registered next to the YAML/CSV providers, and requires **no changes** to the
pipeline, gates, or reports. Until such an adapter exists and is verified, no
platform integration is claimed: private APIs are not invented, platforms are
not scraped, and instruments are never hard-coded as live.
