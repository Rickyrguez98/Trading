# Universe Audit

Status before this review cycle. Written as the first commit of the
universe-expansion work; subsequent commits close each gap below.

## Methodology

- Grepped for every reference to a hard cap on the number of tickers
  processed: `max_tickers`, `--limit`, `.head(`.
- Read `data_providers/ticker_provider.py` to see what the actual source
  files contain.
- Ran the live `NasdaqTraderTickerProvider` once to produce a real
  cross-section of the universe by exchange and asset type.

## Findings

### F1. The "50 tickers" cap was a CLI flag, not a code default

The user reported that the pipeline appeared limited to the top 50
tickers. Tracing the source:

| Location                                                          | Cap        |
|-------------------------------------------------------------------|------------|
| `configs/default_config.yaml::run.max_tickers`                    | **500**    |
| `config.py::RunConfig.max_tickers` (dataclass default)            | **500**    |
| CLI `--limit` (overrides the config)                              | user value |
| `pipelines/run_asset_selection.py::main` line ~161                | applies it |

So the chain is:

```
CLI --limit ──► config.run.max_tickers ──► universe_df.head(N)
```

The user's command included `--limit 50`, so they saw 50. But **even with
no `--limit`, the default `run.max_tickers = 500` would cap the cleaned
4735-stock universe to 500**. That is the design problem: there's an
implicit, silent cap that activates whenever the universe is bigger than
500 — which is always.

Where the 500-cap was applied was also the *wrong* place: it ran *before*
any liquidity or fundamentals filter, meaning we kept the first 500 rows
of the alphabetically-sorted universe and threw away everything from
`F`–`Z`.

### F2. Universe is already multi-exchange (not Nasdaq-only)

The provider `NasdaqTraderTickerProvider` has a misleading name. It pulls
**two** files from NASDAQ Trader (the symbol-directory service, not the
exchange):

- `nasdaqlisted.txt` → NASDAQ-listed only
- `otherlisted.txt`  → everything else: NYSE, NYSE American, NYSE Arca,
                       BATS, IEX

Empirical breakdown from a live fetch (2026-06-05):

| Exchange       | Raw count |
|----------------|-----------|
| NASDAQ         | 5495      |
| NYSE           | 2926      |
| NYSE Arca      | 2657      |
| BATS           | 1398      |
| NYSE American  | 319       |
| IEX            | 3         |
| M (other)      | 1         |
| **Total raw**  | **12799** |

| Asset type     | Count |
|----------------|-------|
| common         | 7470  |
| etf            | 5329  |

After cleaning (ETFs / warrants / units / preferreds / rights / test
issues removed): **~4735 common stocks** across all of the above
exchanges. The cleaning logic in `universe.py` correctly applies to every
source, so multi-exchange coverage is already in place.

### F3. There is no exchange-include knob

The current `UniverseConfig` has `exclude_etfs`, `exclude_warrants`,
`exclude_units`, etc., but no way to say "I only want NYSE + NASDAQ" or
"include preferreds for this run". Exchange filtering is binary
(`exclude_test_issues` only) and you can't whitelist by exchange.

### F4. There are no pipeline stages with size bounds

`run_asset_selection.main` does, per ticker, in a single loop:

```python
for ticker in tickers:
    fund_provider.fetch(ticker)   # expensive
    price_provider.fetch(ticker)  # cheap-ish
    news_provider.fetch(ticker)   # expensive + rate-limited
```

There is no concept of cheap-filter → fundamental-prescreen → news. Every
ticker that survives the universe cap pays for all three calls, which
makes news the binding free-API constraint and means a "full-universe"
run is economically incompatible with the free yfinance news endpoint.

### F5. CLI has no mode concept

Modes the spec calls for — `full`, `sample`, `custom` — are not
distinguished. There is just `--limit N` and `--tickers …`. Without a
mode flag, a user running the default pipeline cannot tell whether they
are getting "the full universe" or "an arbitrary 500-row prefix".

### F6. Output reports do not include reduction stats

`asset_selection_summary.json` lists the final candidates but does not
say:
- how big the raw universe was,
- how many were removed at each stage and why,
- which exchanges contributed,
- which provider calls failed.

This makes it impossible to tell, from the report alone, whether the
"universe" the report claims to cover was actually 4700 or 50.

## What needs to change

| ID | Change | Commit |
|----|--------|--------|
| F1 | Remove the silent 500 default. Make `--limit` opt-in for `sample` mode only. Move the cap out of the alphabetical prefix and into post-liquidity ranking. | "fix: remove default top 50 universe cap" |
| F2 | Document that the existing provider is already multi-exchange. Rename it `NasdaqTraderSymbolDirectoryProvider` is too disruptive — leave the class name but make logs/docs clearer. | "audit: document current universe limitation" |
| F3 | Add `universe.exchanges` whitelist + `include_etfs / funds / warrants / units / preferred / rights` toggles, replacing the current `exclude_*` flags. Keep behaviour-compatible defaults. | "fix: expand ticker universe across US exchanges" |
| F4 | Implement a staged funnel: universe → cheap filters → fundamental prescreen → news/sentiment → final ranking. Each stage carries `top_k` from config and emits stats. | "feat: add staged universe reduction pipeline" |
| F5 | Add `--universe full|sample|custom`. `full` = no implicit cap on stage 1, only the per-stage `top_k`s reduce. `sample` honours `--limit`. `custom` uses `--tickers`. | "fix: remove default top 50 universe cap" (same commit) |
| F6 | Emit per-stage stats in the JSON summary and the Markdown report, including provider-failure counts. | "feat: add staged universe reduction pipeline" (same commit) |

## What is **not** in scope for this review

- Asset allocation, position sizing — still in `docs/FUTURE_ROADMAP.md`.
- Replacing yfinance with a paid provider — the goal is to stay free.
- Point-in-time fundamentals — needed only when backtesting starts.
- A second universe source (SEC EDGAR direct, IEX cloud) — the NASDAQ
  Trader files already cover what the spec asks for; the registry remains
  open so future providers slot in cleanly.

## Recommended posture going forward

- **Default run** (no flags) → `full` universe, staged reduction, news
  fetched only for the few hundred best fundamentals candidates. Long
  but bounded.
- **Iterating on weights / scoring** → `--universe sample --limit 50`
  for fast feedback.
- **Specific shortlist** → `--tickers AAPL MSFT …` for one-off checks.
