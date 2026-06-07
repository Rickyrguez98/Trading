# Ranking Interpretation Audit

_Decision-quality + reporting review of the asset-selection output, before the
"research ranking vs. allocation eligibility" milestone._

> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.

## Why this audit exists

The latest full-universe run completed successfully and produced a technically
valid ranking. The numbers are fine; the **interpretation** is not. The single
`top_candidates.md` table sorts everything by `final_score` and presents the
result as one undifferentiated list. A reader can't tell, at a glance, the
difference between:

- a steady, well-covered compounder we'd actually be comfortable sizing, and
- a 100%-volatility momentum name, or a thin-fundamentals watchlist name that
  only ranks highly because one pillar or a stale sentiment blip lifted it.

This document records the concrete problems so the follow-on commits have a
written baseline. **Nothing here removes candidates** — the fix is to *label and
separate*, never to hide useful research names.

## Issue 1 — The headline table mixes incompatible decision buckets

`scoring/composite_score.py::_selection_bucket` already classifies every
candidate into one of four buckets (`high_quality_core_candidate`,
`growth_candidate`, `speculative_candidate`, `watchlist_only`), but
`scoring/ranking.py::format_top_candidates_markdown` renders them all in one
table sorted by `final_score`. The bucket is just another column.

From the latest top-20 (research ranking), the mixing is obvious:

| Rank | Ticker | Bucket | Why it is not portfolio-ready |
| --- | --- | --- | --- |
| 3 | CDE | watchlist_only | negative recent return, weak trend, high risk |
| 4 | ONDS | speculative_candidate | high volatility |
| 5 | CRDO | speculative_candidate | high volatility + speculative momentum |
| 6 | WDC | speculative_candidate | speculative momentum |
| — | EQT | watchlist_only | weak trend / thin fundamentals |
| — | B | watchlist_only | weak trend / thin fundamentals |

These names are *legitimate research output* — they belong in the ranking — but
presenting them inline with core candidates invites someone to read rank order
as an allocation queue. There is no `eligible_for_allocation` concept anywhere
in the codebase, so "ranked #3" and "suitable to size" look identical.

**Fix direction (commits 2, 5, 6):** add an explicit
`eligible_for_allocation` boolean (default true only for core/growth that clear
risk + data-quality + sentiment thresholds), an `allocation_adjusted_score`, and
split the report into a portfolio-eligible shortlist, a research ranking, a
speculative section, a watchlist section, and an excluded-reasons section.

## Issue 2 — `final_score` uses RAW sentiment, not confidence-adjusted

`scoring/composite_score.py::compute_composite_scores` reads the raw
`sentiment_score` column directly:

```python
sentiment = pd.to_numeric(df.get("sentiment_score", 50), errors="coerce").fillna(50)
...
+ w.get("sentiment", 0.0) * sentiment
```

The sentiment pipeline (`sentiment/sentiment_model.py`) already computes a rich
`confidence` in `[0,1]` (a five-factor blend of unique article volume, source
diversity, freshness, de-duplication, and a model-quality cap) **and** a
`fresh_ratio`. None of it touches the composite. Confidence is *reported* but
never *applied*.

Consequence: a name like GEN — high headline sentiment off a thin, low-
confidence, possibly stale feed — gets the same sentiment contribution to
`final_score` as a name with 25 unique articles from 5 sources. A single
optimistic wire story is weighted exactly like a genuine consensus.

**Fix direction (commit 3):** compute
`effective_sentiment_score = neutral + confidence * (raw - neutral)` and feed the
**effective** value into the composite by default (config-gated via
`use_confidence_adjusted_sentiment`), while keeping `raw_sentiment_score` in the
output for transparency. Low confidence pulls sentiment toward neutral (50)
instead of letting it swing the score.

## Issue 3 — Stale news is reported but not down-weighted

`validation/output_validation.py::_check_stale_news` flags stale coverage
*after the fact*, and `aggregate_ticker_sentiment` records `stale_count` /
`fresh_ratio`, but staleness never reduces the sentiment's influence on the
score. A candidate whose only news is three weeks old still gets full-strength
sentiment in `final_score`.

There are also no candidate-level `STALE_NEWS` / `VERY_STALE_NEWS` /
`LOW_SOURCE_DIVERSITY` flags — `flag_rows` emits `LOW_SENTIMENT_CONFIDENCE` and
`NO_NEWS` but nothing about freshness or source breadth.

**Fix direction (commit 4):** add the freshness/diversity flags, and let a low
`fresh_ratio` reduce the *effective* confidence used in Issue 2's formula, so
stale sentiment is automatically damped toward neutral.

## Issue 4 — Provider reporting is inconsistent across artifacts

The four output artifacts disagree about what providers were used:

- `asset_selection_summary.json` reports `providers: {"prices": "yfinance", ...}`
  — a single configured name per data type
  (`run_asset_selection.py::_build_summary`).
- `provider_diagnostics.md` renders a fallback-usage table showing the chain
  (e.g. `yfinance, stooq` for prices) plus Primary/Fallback/Stale-cache columns.
- For **unwrapped** single providers, that table shows `Primary=0, Fallback=0`
  (`_fallback_usage_summary` returns `wrapped: False` with no counters), which
  reads as "the provider was never used" even though it returned 100% of the
  fundamentals/news. The zeros are an artifact of only `Fallback*` wrappers
  exposing usage counters, not a real measurement.

So a reader comparing the summary JSON and the diagnostics MD sees a different
provider story, and the diagnostics table implies the providers did nothing.

**Fix direction (commit 7):** report a single consistent set of fields
everywhere — `configured_providers` (the resolved chain per data type),
`actual_provider_usage` (what really served the data, from cache provenance /
fallback counters), `provider_chain_by_data_type`, and `cache_usage_by_stage`.
For unwrapped providers, show `n/a` for fallback counters (never a misleading
`0`) and derive actual usage from the per-record `data_source` provenance.

## Issue 5 — JSON `candidates` is ambiguous (top-N vs. ranked count)

`asset_selection_summary.json` carries a `candidates` array that only holds the
reported top-N (`_build_summary` slices `ranked.head(config.run.top_n)`), while
the run actually ranked many more (e.g. 150). There is no field that states the
*ranked* count vs. the *reported* count, and no pointer to the full CSV. A
downstream consumer can't tell whether `candidates` is the whole ranking or a
preview.

**Fix direction (commits 5, 9):** add `ranked_candidate_count`,
`reported_candidate_count`, `top_n`, `full_results_path`, and
`top_candidates_path`, and document that `candidates` is the reported top-N
slice (the complete ranking lives in the CSV).

## Guardrails for the fixes

These are the user's standing rules; every follow-on commit must respect them:

- Do **not** hide speculative or watchlist candidates — label and separate.
- Do **not** delete useful research candidates.
- Do **not** present speculative/watchlist names as portfolio-ready by default.
- Do **not** let stale or low-confidence sentiment over-influence `final_score`.
- Do **not** implement asset allocation or portfolio optimization yet — only
  prepare the asset-selection output so it can safely feed a future
  allocation/rebalancing module.

Asset selection answers **"which assets are worth considering?"** A later
allocation/rebalancing module answers **"how much capital, and when to adjust?"**
This milestone keeps those two questions visibly separate in the output.
