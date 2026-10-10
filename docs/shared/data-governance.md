# Data governance

## Layout and ownership

```
data/us_equities/{input,raw,processed,cache}/
data/binary_options/{input,raw,processed,cache}/
reports/us_equities/
reports/binary_options/{runs/<run_id>/, latest/}
```

- Each domain owns its tree; cross-domain writes are prohibited (test-enforced).
- `input/` holds user-maintained files (e.g. the verified instrument
  catalog). `processed/` and `reports/` are generated and git-ignored;
  placeholders (`.gitkeep`) keep the layout present.
- Binary-options run artifacts are **immutable per run** under
  `runs/<run_id>/`; `latest/` is a refreshed copy — history is never
  silently overwritten.

## Timestamps

Aware-UTC internally everywhere; exchange-local times only at reporting
edges. Candle boundaries are deterministic (`[start, start + interval)`;
complete iff the window elapsed). DST is handled by the IANA timezone
database (`zoneinfo`), covered by tests for EST/EDT transitions.

## Provenance and reproducibility

Every binary-options run writes `run_manifest.json`: git commit + dirty
state, python/package versions, configuration path + sha256 + full snapshot,
providers + versions, catalog path + sha256, requested horizons/interval,
timezone, output paths, run status. Manifests contain **no secrets**.

## Retention and licensing

Free/public sources only by default; we cache to be polite guests, respect
rate limits, and never scrape past terms of service. Users who publish
results should attribute the underlying data sources. Local caches and
generated reports may be deleted at any time (`make clean` removes caches
and generated outputs; run history removal is deliberate and manual).
