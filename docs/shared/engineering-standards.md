# Engineering standards

## Domain boundaries

- `asset_selection` (US Equities) and `binary_options_selection` (Binary
  Options) are independent research products. They never import each other's
  logic; the only sanctioned link is the equity output CSV consumed as an
  upstream **universe filter** (explicitly not a directional predictor).
- `trading_common` holds **generic infrastructure only**: on-disk cache,
  rate limiting, IO helpers, transport error taxonomy. No domain constants,
  no domain imports (enforced by
  `tests/integration/test_domain_separation.py`).
- Configuration, data, cache, and report paths are domain-separated
  (`configs/`, `data/`, `reports/` each split by domain) and must never
  collide (also test-enforced).

## Honesty rules (non-negotiable)

- Never fabricate: no invented candles, forward-fills, resampled "finer"
  data, fabricated contract availability, or fabricated payout data.
- Missing data is reported and gated, never silently neutral-filled.
- Delayed feeds are labeled delayed; stale cache is never served as live.
- Heuristic scores are never called probabilities; expected value is never
  derived from them.
- Every exclusion carries a machine-readable reason; a degraded run is
  visibly different from a valid one (`VALID`/`PARTIAL`/`DEGRADED`/`INVALID`).
- No execution, no broker automation, no credential storage, no stake
  advice, no martingale/loss-recovery logic — ever.

## Configuration

YAML-first with typed dataclass trees; unknown keys tolerated; semantic
validation reports **all** problems at once. Legacy paths/keys keep working
through shims that emit `DeprecationWarning` pointing at
[migration.md](migration.md).

## Testing and CI

- The normal suite is deterministic and fully offline (scripted providers,
  injectable clocks); live-provider tests would be optional and explicitly
  marked (none exist today).
- Run tests before every commit; never claim a check passed without running
  it. CI (`.github/workflows/quality.yml`) runs lint + the offline suite +
  packaging import checks on pushes and PRs and depends on no market APIs.

## Secrets

Read from environment variables only (`.env` via `python-dotenv`;
`.env.example` documents names, never values). Nothing secret is committed,
logged, or written into manifests.

## Git

Single human identity (see [git-authorship.md](git-authorship.md)); atomic
commits; no history rewrites of published branches; feature branches merged
by PR.
