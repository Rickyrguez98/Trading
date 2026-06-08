# Sentiment Models — VADER, FinBERT, and Comparison Mode

> Research output only. Not financial advice. See [`DISCLAIMER.md`](DISCLAIMER.md).

This pipeline scores news **headlines + summaries** per ticker and rolls them up
into a single `sentiment_score` on a `0..100` scale (50 = neutral). Two backends
are supported, and they can be run **side by side** to compare them.

| | **VADER** (default) | **FinBERT** (optional) |
| --- | --- | --- |
| Type | Lexicon / rule-based | Transformer (BERT) fine-tuned on financial text |
| Finance-tuned? | **No** — general English | **Yes** — `ProsusAI/finbert` |
| Dependencies | none (always installed) | `transformers` + `torch` (the `[finbert]` extra) |
| Cost | instant, CPU-cheap | ~440 MB download, slow on CPU |
| Confidence ceiling | `vader_confidence_factor` (0.85) | `finbert_confidence_factor` (1.0) |

## Why FinBERT can be better for finance

VADER is a general-purpose lexicon. It does not know that "**beat** estimates",
"**miss**", "**guidance cut**", "**downgrade**", "**dilution**", or "**default**"
are loaded *financial* terms, and it can misread Wall-Street idiom (e.g. a
"**short** position", a stock that "**tanked**", a "**bullish**" call). FinBERT is
fine-tuned on financial news and filings, so it generally classifies that
vocabulary more accurately. That is exactly why we let you **compare** the two
before trusting either: where they agree, you have a robust read; where they
disagree, the ticker is flagged for a human, not silently averaged away.

Because FinBERT is heavy and optional, **VADER remains the default** so the base
project runs with zero extra dependencies.

## Installing the optional FinBERT extras

```bash
pip install -e ".[finbert]"      # adds transformers + torch
```

FinBERT downloads its weights (`ProsusAI/finbert`, ~440 MB) on first use. If the
extras are **not** installed, nothing breaks — the run uses VADER and says so
(see *Safe fallback* below). FinBERT results are **never fabricated**.

## Running each mode

All of these are set in `configs/default_config.yaml` under `sentiment:`.

**1. VADER only (default):**
```yaml
sentiment:
  model: vader
```

**2. FinBERT only:**
```yaml
sentiment:
  model: finbert
  fallback_to_vader_if_finbert_unavailable: true   # degrade to VADER if missing
```

**3. Comparison (run BOTH, compare per ticker):**
```yaml
sentiment:
  model: comparison            # or keep model: vader and set compare_models: true
  compare_models: true
  final_sentiment_source: vader  # which score feeds the composite: vader | finbert | ensemble
  ensemble_vader_weight: 0.5
  ensemble_finbert_weight: 0.5
  sentiment_disagreement_threshold: 25.0   # |vader - finbert| (0..100) above this = "disagree"
  low_finbert_confidence_threshold: 0.30
```

Then run normally:
```bash
asset-selection --universe sample --limit 50
```

## What you get per article and per ticker

**Per article** (carried on each scored record in comparison runs):
`vader_score`, `finbert_score`, `vader_label`, `finbert_label`, `model_used`.

**Per ticker** (columns in `data/processed/asset_selection_results.csv` and the
`candidates[]` block of `reports/asset_selection_summary.json`):
`vader_sentiment_score`, `finbert_sentiment_score`, `sentiment_score_delta`,
`sentiment_model_agreement`, `final_sentiment_score`, `sentiment_model_used`.

**Run level** (`sentiment_summary` in the JSON and the *Sentiment model* block at
the top of `reports/top_candidates.md`): the model used, whether FinBERT was
available, how many articles each model scored, the average VADER / FinBERT
score, the model-disagreement count, and the tickers with large disagreements.

## Final-score selection logic

| `model` | FinBERT available? | Final score |
| --- | --- | --- |
| `vader` | n/a | VADER |
| `finbert` | yes | FinBERT |
| `finbert` | no, `fallback_to_vader…: true` | VADER (+ `FINBERT_UNAVAILABLE`, `VADER_ONLY_SENTIMENT`) |
| `finbert` | no, fallback off | neutral 50, `sentiment_model_used = none` (never faked) |
| `comparison` | yes | `final_sentiment_source`: `vader` / `finbert` / `ensemble` (weighted blend) |
| `comparison` | no | VADER (+ `FINBERT_UNAVAILABLE`, `VADER_ONLY_SENTIMENT`) |

## Flags

| Flag | Meaning |
| --- | --- |
| `SENTIMENT_MODEL_DISAGREEMENT` | `|vader − finbert|` exceeds `sentiment_disagreement_threshold` for the ticker. |
| `FINBERT_UNAVAILABLE` | FinBERT was requested but the extras/model could not be loaded. |
| `VADER_ONLY_SENTIMENT` | Only VADER scored this ticker (FinBERT unavailable). |
| `LOW_FINBERT_CONFIDENCE` | Mean per-article FinBERT confidence is below `low_finbert_confidence_threshold`. |

## Safe fallback (FinBERT not installed)

If `transformers` / `torch` are missing, or the model fails to load (offline,
out-of-memory), the pipeline:

1. logs a clear warning naming the cause and the fix (`pip install '.[finbert]'`),
2. scores with **VADER**,
3. sets `FINBERT_UNAVAILABLE` (+ `VADER_ONLY_SENTIMENT`) on affected tickers,
4. records `finbert_available: false` and the reason in `sentiment_summary`,
5. prints the same in the *Sentiment model* block of `top_candidates.md`.

No step invents a FinBERT number.

## Why sentiment stays secondary to fundamentals

Sentiment is **bounded and confidence-adjusted** so it can never dominate:

- **Weighting.** In the composite, fundamentals-family weights total **0.85**
  (`fundamentals 0.50`, `growth 0.15`, `quality 0.10`, `valuation 0.10`) while
  `sentiment` is **0.15**. Fundamentals move `final_score` far more than
  sentiment does (asserted by `test_fundamentals_dominate_sentiment_under_default_weights`).
- **Confidence adjustment.** With `use_confidence_adjusted_sentiment: true`, the
  composite consumes `effective_sentiment = neutral + confidence·(raw − neutral)`,
  so a thin, single-source, stale, or low-confidence feed is pulled back toward
  neutral instead of swinging the score. `raw_sentiment_score` is still reported
  for transparency.
- **Model ceiling.** VADER's confidence is capped below FinBERT's, and stale news
  damps confidence further — so even a confident-looking lexicon read is throttled.

The net effect: **changing the sentiment model changes a small, bounded input.**
It is a tie-breaker and a risk flag, never the thesis.
