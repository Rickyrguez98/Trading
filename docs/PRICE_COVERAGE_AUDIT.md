# Price Coverage & Critical-Ticker Audit

_Data-quality review of the Stage-2 price funnel: why important liquid tickers can
be dropped, and what structured fallback should happen before classifying a name
as `NO_PRICE_DATA`._

> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.

## Why this audit exists

A full-universe run reported **price coverage 4723/4731 = 99.83%** and a
**VALID** run status, yet eight liquid, important tickers were dropped in Stage 2
as `NO_PRICE_DATA`:

    CRDO  GEN  NVDA  ONDS  WDC  CDE  EQT  RDDT

NVDA failing *silently* — as if illiquid, unsupported, or delisted — is the
signature of a data-quality problem, not an economic one. A 99.83% headline that
hides a missing NVDA is exactly the kind of coverage figure that should **not**
be trusted at face value. This document records the concrete root causes so the
follow-on commits have a written baseline. **Nothing here drops or hides a
candidate** — the fix is to *resolve symbols harder, record what was actually
tried, classify failures honestly, and flag material gaps loudly.*

## What actually happens in the code today

### Finding 1 — yfinance normalization is already correct; the *reporting* hides it

`data_providers/symbols.py::to_provider_symbol` maps:

| canonical | yfinance | Stooq (`stooq_provider._stooq_symbol`) |
| --- | --- | --- |
| `NVDA` | `NVDA` | `nvda.us` |
| `RDDT` | `RDDT` | `rddt.us` |
| `BRK.B` | `BRK-B` | `brk-b.us` |

So yfinance is **never** handed `nvda.us`. The `nvda.us` spelling that shows up
in the diagnostics is the **Stooq** provider symbol. Because the fallback chain
(`data_providers/fallback.py::_run_chain`) returns only the **last** provider's
record on total failure, the surviving `provider_symbol` is Stooq's `nvda.us` —
which *reads* as "yfinance tried nvda.us and failed." The normalization is fine;
the **chain collapses the per-provider attempts** so the report is misleading.

### Finding 2 — the chain records no per-provider attempt trail

`_run_chain` keeps aggregate counters (`primary`, `fallback`, `unavailable`) but
throws away the per-attempt detail. For a failed ticker we cannot answer:

- Did yfinance fail, Stooq fail, or both?
- Which provider symbol / variant was attempted for each?
- Was it a transport error (blocked/JSON-parse) or an honest empty payload?

Everything collapses into a single `NO_PRICE_DATA`, so an outage and a genuinely
unknown symbol look identical.

### Finding 3 — no symbol-resolution ladder

Each provider tries exactly **one** symbol. There is no structured ladder of
variants (direct → provider-normalized → class-share → alias) and no
cross-provider confirmation, so a single empty payload ends the attempt even for
a name a small spelling change would have resolved.

### Finding 4 — `NO_PRICE_DATA` / "possibly delisted" is too broad for mega-caps

`symbols.py::likely_no_data_reason` returns "...symbol may be delisted, halted,
recently listed, or not covered by the provider." For NVDA that wording is
misleading: if fundamentals/news exist for the same canonical ticker, a price
miss is a **price-endpoint / provider gap**, not evidence of delisting.

### Finding 5 — coverage-only validation hides material gaps

`validation/coverage.py` gates on aggregate ratios
(`min_price_coverage_ratio`, provider-failure budget, valid-candidate count).
99.83% clears every threshold, so a run that silently dropped NVDA is still
`VALID_RANKING`. There is **no concept of a critical/material ticker** and no
`ranking_completeness_status`, so materiality never enters the verdict.

### Finding 6 — the real cause for all eight, in this environment

In the sandboxed environment, both yfinance and Stooq are network-blocked and
return HTML/empty instead of JSON/CSV. Driven through the real classifiers, all
eight tickers fail with a **provider-side** error
(`PROVIDER_JSON_PARSE_ERROR` / `PROVIDER_BLOCKED`) on **both** providers — i.e. a
**provider/transport gap**, not a per-ticker symbol problem and not delisting.
The fix surfaces that distinction (per-provider attempts + honest classification)
and refuses to let it disappear behind a 99.83% headline (materiality).

## Fix direction (follow-on commits)

1. **Symbol-resolution ladder + alias map** (`symbols.py`):
   `resolve_provider_symbols(canonical, provider, metadata)` returns an ordered,
   provider-specific list of variants. Keep `canonical` separate from
   `provider_symbol`; never let a Stooq `.us` form overwrite the yfinance symbol.
2. **Per-provider attempt trail** (`base.py`, providers, `fallback.py`):
   record `canonical_symbol, provider_name, provider_symbol,
   symbol_variant_attempted, success, error_type, error_message,
   response_summary` for each attempt; the chain concatenates across providers.
   The ladder only spends extra calls on *empty* results, never on transport
   errors (where variants cannot help).
3. **Critical tickers + Stage-2 recovery** (`config.py`, `criticality.py`,
   stage 2): a configurable critical set (+ dynamic large-cap / high-liquidity /
   watchlist), a full ladder + cross-provider fundamentals confirmation for
   failed critical names, and a `material_data_gap` label instead of a silent
   drop. Cross-provider evidence reclassifies "delisted-ish" → `PRICE_PROVIDER_GAP`.
4. **Materiality-based validation** (`coverage.py`): add
   `critical_ticker_failures`, `material_data_gaps`,
   `failed_high_liquidity_tickers`, `failed_large_cap_tickers`,
   `failed_user_watchlist_tickers`, and a `ranking_completeness_status`
   (`COMPLETE` / `COMPLETE_WITH_MINOR_GAPS` / `VALID_WITH_MATERIAL_WARNINGS` /
   `PARTIAL_CRITICAL_TICKER_FAILURE` / `INVALID_SYSTEMIC_PROVIDER_FAILURE`). A
   single critical miss does **not** invalidate the whole run, but it downgrades
   completeness and is reported loudly.
5. **Error taxonomy** (`errors.py`): add `PRICE_ENDPOINT_NO_DATA`,
   `PROVIDER_SYMBOL_RESOLUTION_FAILED`, `PRICE_PROVIDER_GAP`,
   `PROVIDER_COVERAGE_GAP`, `CRITICAL_TICKER_PRICE_FAILURE`; never assert
   delisting for a well-known ticker without evidence.
6. **Consistent diagnostics** (`provider_diagnostics.py`): render the
   per-provider attempt trail and the materiality block; reconcile
   primary/fallback counts with live/unavailable totals.

## Guardrails for the fixes

- Do **not** claim NVDA has no price data unless yfinance direct + every
  configured fallback were actually tried (and recorded).
- Do **not** use Stooq-style symbols for yfinance.
- Do **not** let overall coverage hide material missing tickers.
- Do **not** classify important liquid tickers as possibly delisted without
  evidence.
- Do **not** remove the research-ranking vs. allocation-shortlist separation.
- Do **not** implement asset allocation or live trading — keep this milestone on
  better data coverage and safer validation.
