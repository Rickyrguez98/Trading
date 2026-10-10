# Migration guide — flat layout → domain-separated platform

This release reorganized the repository into two clearly separated research
products. Old paths keep working where a safe shim exists; each shim emits a
`DeprecationWarning` naming this guide.

## Configuration files

| Old | New |
|---|---|
| `configs/default_config.yaml` | `configs/us_equities/default.yaml` |
| `configs/binary_options_config.yaml` | `configs/binary_options/default.yaml` |

Both loaders resolve the new canonical path first and still honour the legacy
flat path (with a warning). The `ASSET_SELECTION_CONFIG` /
`BINARY_OPTIONS_CONFIG` environment variables are unchanged.

## Output, data, and cache paths

| Old (shared) | New (domain-separated) |
|---|---|
| `reports/` | `reports/us_equities/`, `reports/binary_options/` |
| `data/processed/` | `data/us_equities/processed/`, `data/binary_options/processed/` |
| `data/cache/` | `data/us_equities/cache/`, `data/binary_options/cache/` |

YAMLs that still contain the flat values (`reports`, `data/processed`) are
migrated onto the domain layout at load time so the two products can never
write into each other's outputs.

## Binary-options output files

| Old | New |
|---|---|
| `data/processed/binary_options_candidates.csv` | `data/binary_options/processed/signals_{all,short_term,extended_intraday}.csv` |
| `reports/binary_options_candidates.md` | `reports/binary_options/latest/signals_{all,short_term,extended_intraday}.md` |
| `reports/binary_options_summary.json` | `reports/binary_options/latest/run_summary.json` |
| `reports/binary_options_provider_diagnostics.*` | `reports/binary_options/latest/provider_diagnostics.*` |
| `reports/binary_options_output_validation.*` | `reports/binary_options/latest/output_validation.*` |
| — | `reports/binary_options/latest/run_manifest.json` (new) |
| — | `reports/binary_options/runs/<run_id>/` (immutable history, new) |

## Horizon groups (breaking, by product definition)

* `short_term` = **5, 10, 15** minutes (was `short` = 5–30).
* `extended_intraday` = **30, 60, 90, 120** minutes — **30 moved here**.
* `weights.short` in YAML is a deprecated alias of `weights.short_term`
  (warned, still accepted).

## Universe modes (behavioral)

* Default universe mode is now **`catalog`**: instruments come from your
  verified platform catalog; without one, every row is
  `NO_TRADE / INSTRUMENT_AVAILABILITY_UNKNOWN`.
* `custom` / `watchlist` / `asset-selection` analyze generic symbols and now
  require `run.research_mode: true` (or `--research-mode`).

## NO_TRADE reason renames (canonical taxonomy)

| Old value | New value |
|---|---|
| `SCORES_TOO_CLOSE` | `WEAK_DIRECTIONAL_MARGIN` |
| `STALE_DATA` | `STALE_MARKET_DATA` |
| `EXCESSIVE_MISSING_CANDLES` | `MISSING_CANDLES` |
| `NO_COMPLETED_CANDLE` | `INCOMPLETE_LATEST_CANDLE` |
| `PROVIDER_COVERAGE` | `PROVIDER_UNAVAILABLE` |
| `ABNORMAL_DATA` | `INVALID_OHLC` |
| `NO_COMPUTABLE_FEATURES` | `INSUFFICIENT_FEATURE_COVERAGE` |

Python constant names (`NO_TRADE_*`) were preserved; only the string values
changed.

## CLI commands

Canonical: `trading-us-equities`, `trading-binary-options`.
Kept as aliases: `asset-selection`, `binary-options-selection`, and the
`python -m asset_selection.pipelines.run_asset_selection` /
`python -m binary_options_selection.pipelines.run_binary_options_selection`
module forms.

## Documentation and tests

Docs moved to `docs/{shared,us_equities,binary_options}/` (see
[INDEX.md](../INDEX.md)); tests moved to
`tests/{common,us_equities,binary_options,integration}/`.
