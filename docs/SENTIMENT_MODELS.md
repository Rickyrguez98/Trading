# Sentiment Models — VADER, FinBERT, Comparison, and Ensemble

> Research output only. Not financial advice. See [`DISCLAIMER.md`](DISCLAIMER.md).

This pipeline scores news **headlines + summaries** per ticker and rolls them up
into a single `sentiment_score` on a `0..100` scale (50 = neutral). Two backends
are supported — a fast lexicon (**VADER**) and an optional finance-tuned
transformer (**FinBERT**) — and they can be run **side by side** to compare or
**blend** them. VADER is always the default so the base project runs with **zero
extra dependencies** and FinBERT is **never required and never fabricated**.

## 1. The two backends at a glance

| | **VADER** (default) | **FinBERT** (optional) |
| --- | --- | --- |
| Type | Lexicon / rule-based | Transformer (BERT) fine-tuned on financial text |
| Finance-tuned? | **No** — general English | **Yes** — `ProsusAI/finbert` |
| Dependencies | none (always installed) | `transformers` + `torch` (the `[finbert]` extra) |
| First-run cost | instant, CPU-cheap | ~440 MB weight download, then slow on CPU |
| Output | compound ∈ `[-1, 1]` | softmax over `{positive, neutral, negative}` |
| Confidence ceiling | `vader_confidence_factor` (0.85) | `finbert_confidence_factor` (1.0) |

Both backends are normalised to the same `0..100` per-article scale before
aggregation. For FinBERT the per-article score is
`50 + 50 · (positive_probability − negative_probability)`, clamped to
`[0, 100]`, so its output is drop-in compatible with the existing roll-up.

## 2. Why FinBERT can be better for finance

VADER is a general-purpose lexicon. It does not know that "**beat** estimates",
"**miss**", "**guidance cut**", "**downgrade**", "**dilution**", or "**default**"
are loaded *financial* terms, and it can misread Wall-Street idiom (e.g. a
"**short** position", a stock that "**tanked**", a "**bullish**" call). FinBERT is
fine-tuned on financial news and filings, so it generally classifies that
vocabulary more accurately. That is exactly why we let you **compare** the two
before trusting either: where they agree, you have a robust read; where they
disagree, the ticker is flagged for a human, not silently averaged away.

Because FinBERT is heavy and optional, **VADER remains the default** so the base
project runs with no extra dependencies.

## 3. Installing the optional FinBERT extras

```bash
pip install -e ".[finbert]"          # adds transformers + torch
# or:
pip install -r requirements-finbert.txt
```

FinBERT downloads its weights (`ProsusAI/finbert`, ~440 MB) on first use, so the
first run needs network access. If the extras are **not** installed, nothing
breaks — the run uses VADER and says so (see *§10 Safe fallback*). FinBERT
results are **never fabricated**.

## 4. Running each mode

Set these in `configs/default_config.yaml` under `sentiment:`, **or** override the
model at the command line with `--sentiment-model {vader,finbert,comparison,ensemble}`
(the CLI flag wins over the YAML `model:`).

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

**3. Comparison (run BOTH, compare per ticker, pick which feeds the composite):**
```yaml
sentiment:
  model: comparison              # or keep model: vader and set compare_models: true
  compare_models: true
  final_sentiment_source: vader  # which score feeds the composite: vader | finbert | ensemble
  model_disagreement_threshold: 20.0   # |vader - finbert| (0..100): at/above = strong disagreement
  low_finbert_confidence_threshold: 0.30
```

**4. Ensemble (run both, feed a weighted blend into the composite):**
```yaml
sentiment:
  model: ensemble                # forces final_sentiment_source = ensemble
  ensemble_vader_weight: 0.4
  ensemble_finbert_weight: 0.6
```
The blend is `weight·score` normalised by the total weight, e.g. with the
defaults a VADER `100` and FinBERT `0` produce `0.4·100 = 40`. If FinBERT is
unavailable the ensemble degrades to VADER (and flags it) rather than inventing a
FinBERT term.

Then run normally — for example a quick sample:
```bash
asset-selection --universe sample --limit 50 --top 20 \
  --provider prices=yfinance,stooq --sentiment-model comparison
```

## 5. Device selection and batching (FinBERT only)

FinBERT inference picks a device with `sentiment.finbert_device`:

| Setting | Behaviour |
| --- | --- |
| `auto` (default) | Prefer CUDA → Apple-Silicon MPS → CPU, whichever is available. |
| `cuda` / `mps` / `cpu` | Force that device; if the requested accelerator is unavailable it **degrades to CPU** (never crashes). |

Throughput knobs (only used once a real FinBERT model loads):

- `finbert_batch_size` (default `8`) — articles per forward pass; higher is
  faster but uses more memory.
- `finbert_max_length` (default `128`) — token truncation length for
  headline + summary.

The resolved device is reported as `finbert_device_used` in the candidate rows
and `sentiment_summary`, and printed in the *Sentiment model* block of
`reports/top_candidates.md`.

## 6. What you get per article and per ticker

**Per article** (carried on each scored record in comparison/ensemble runs):
`vader_score`, `finbert_score`, `vader_label`, `finbert_label`, `model_used`,
plus the raw FinBERT class probabilities `finbert_positive_probability`,
`finbert_neutral_probability`, `finbert_negative_probability`, and
`scoring_error` (set only if that batch failed — the score is **not** faked).

**Per ticker** (columns in `data/processed/asset_selection_results.csv` and the
`candidates[]` block of `reports/asset_selection_summary.json`):
`vader_sentiment_score`, `finbert_sentiment_score`, `sentiment_score_delta`,
`sentiment_model_agreement` (one of the seven categories in §7),
`final_sentiment_score`, `sentiment_model_used`, the mean FinBERT probabilities,
`finbert_device_used`, `sentiment_model_fallback_used`, and
`finbert_scoring_error_count`.

**Run level** (`sentiment_summary` in the JSON and the *Sentiment model* block of
`reports/top_candidates.md`): `model`, `final_sentiment_source`,
`finbert_available`, `finbert_model_name`, `finbert_device_used`, how many
articles each model scored, the average VADER / FinBERT score, the
`agreement_breakdown` (counts per category), the strong-disagreement count
(`sentiment_model_disagreement_count`), the `mild_disagreement_count`,
`fallback_to_vader_used`, `finbert_scoring_error_count`, and the tickers with
large disagreements.

## 7. Agreement categories

Every comparison/ensemble ticker is classified into exactly one of **seven**
categories (`sentiment_model_agreement`), counted in `agreement_breakdown`:

| Category | Meaning |
| --- | --- |
| `agreement_positive` | Both models read the ticker as positive (same side, within threshold). |
| `agreement_neutral` | Both models near neutral and in agreement. |
| `agreement_negative` | Both models read the ticker as negative. |
| `mild_disagreement` | `|vader − finbert|` is at/above **half** the threshold but below it (no flag). |
| `strong_disagreement` | `|vader − finbert|` is **at/above** `model_disagreement_threshold` → raises `SENTIMENT_MODEL_DISAGREEMENT`. |
| `finbert_unavailable` | FinBERT was **requested** but could not score this ticker. |
| `vader_only` | FinBERT was **never requested** for this ticker (plain VADER run). |

Only `strong_disagreement` raises the disagreement flag; mild divergence is
recorded for transparency but does not flag.

## 8. Final-score selection logic

| `model` | FinBERT available? | Final score |
| --- | --- | --- |
| `vader` | n/a | VADER |
| `finbert` | yes | FinBERT |
| `finbert` | no, `fallback_to_vader…: true` | VADER (+ `FINBERT_UNAVAILABLE`, `VADER_ONLY_SENTIMENT`, `SENTIMENT_MODEL_FALLBACK`) |
| `finbert` | no, fallback off | neutral 50, `sentiment_model_used = none` (never faked) |
| `comparison` | yes | `final_sentiment_source`: `vader` / `finbert` / `ensemble` |
| `comparison` | no | VADER (+ `FINBERT_UNAVAILABLE`, `VADER_ONLY_SENTIMENT`) |
| `ensemble` | yes | weighted blend `(w_v·vader + w_f·finbert) / (w_v + w_f)` (+ `ENSEMBLE_SENTIMENT`) |
| `ensemble` | no | VADER (+ `FINBERT_UNAVAILABLE`, `VADER_ONLY_SENTIMENT`, `SENTIMENT_MODEL_FALLBACK`) |

## 9. Flags

| Flag | Meaning |
| --- | --- |
| `SENTIMENT_MODEL_DISAGREEMENT` | `|vader − finbert|` is at/above `model_disagreement_threshold` for the ticker (strong disagreement only). |
| `FINBERT_UNAVAILABLE` | FinBERT was requested but the extras/model could not be loaded. |
| `VADER_ONLY_SENTIMENT` | Only VADER scored this ticker (FinBERT unavailable). |
| `LOW_FINBERT_CONFIDENCE` | Mean per-article FinBERT confidence is below `low_finbert_confidence_threshold`. |
| `FINBERT_SCORING_ERROR` | A real FinBERT model loaded but one or more batches errored; those articles are **not** scored or faked. |
| `SENTIMENT_MODEL_FALLBACK` | The intended source (finbert/ensemble) was unusable, so the score fell back to VADER. |
| `ENSEMBLE_SENTIMENT` | The composite consumed a weighted VADER+FinBERT blend for this ticker. |

## 10. Safe fallback (FinBERT not installed)

If `transformers` / `torch` are missing, or the model fails to load (offline,
out-of-memory), the pipeline:

1. logs a clear warning naming the cause and the fix (`pip install '.[finbert]'`),
2. scores with **VADER**,
3. sets `FINBERT_UNAVAILABLE` (+ `VADER_ONLY_SENTIMENT`, and
   `SENTIMENT_MODEL_FALLBACK` when finbert/ensemble was the intended source) on
   affected tickers,
4. records `finbert_available: false` and `articles_scored_finbert: 0` in
   `sentiment_summary`,
5. prints the same in the *Sentiment model* block of `top_candidates.md`.

No step invents a FinBERT number. The output validation layer independently
**errors** if a `finbert_sentiment_score` ever appears while `finbert_available`
is false and `articles_scored_finbert == 0` (the `finbert_availability`
output-validation check).

## 11. Why sentiment stays secondary to fundamentals

Sentiment is **bounded and confidence-adjusted** so it can never dominate:

- **Weighting.** In the composite, fundamentals-family weights total **0.85**
  (`fundamentals 0.50`, `growth 0.15`, `quality 0.10`, `valuation 0.10`) while
  `sentiment` is **0.15**. Fundamentals move `final_score` far more than
  sentiment does (asserted by `test_fundamentals_dominate_sentiment_under_default_weights`).
  The `sentiment_dominance` output check re-audits the weights and **errors** if
  sentiment ever meets or exceeds the fundamentals or fundamentals-family weight.
- **Confidence adjustment.** With `use_confidence_adjusted_sentiment: true`, the
  composite consumes `effective_sentiment = neutral + confidence·(raw − neutral)`,
  so a thin, single-source, stale, or low-confidence feed is pulled back toward
  neutral instead of swinging the score. `raw_sentiment_score` is still reported
  for transparency.
- **Model ceiling.** VADER's confidence is capped below FinBERT's, and stale news
  damps confidence further — so even a confident-looking lexicon read is throttled.

The net effect: **changing the sentiment model changes a small, bounded input.**
It is a tie-breaker and a risk flag, never the thesis.

## 12. Known limitations

- **Headlines + summaries only.** FinBERT sees the same short text VADER does, not
  full article bodies, so subtle context in the body is not captured.
- **First-run download & CPU speed.** FinBERT pulls ~440 MB on first use and is
  slow on CPU; for large universes prefer a GPU/MPS device or keep VADER.
- **News breadth is the real bottleneck.** Both models are limited by the narrow,
  free news feed (caps at ~10 articles/ticker). A better model cannot fix a thin
  or stale feed — confidence damping reflects that.
- **FinBERT is three-class, not a price signal.** It classifies tone
  (positive/neutral/negative), which is **not** a forecast of returns; treat it as
  a qualitative, secondary input.
- **No look-ahead control on the news source.** Article timestamps come from the
  provider; backtest-grade point-in-time guarantees are out of scope for this
  milestone.
- **Ensemble weights are heuristic.** The default `0.4 / 0.6` VADER/FinBERT split
  is a sensible prior, not a tuned/learned weight.
- **Offline = VADER.** Without network access on first run FinBERT cannot download
  its weights, so the pipeline degrades to VADER and reports it honestly.
